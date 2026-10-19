"""Prepare and explicitly start independent character instances."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import secrets
import shlex
import subprocess
import tempfile
import tomllib
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlsplit
from uuid import uuid4

HERE = Path(__file__).resolve().parent
CAPABILITIES = [
    "control.character.read",
    "control.character.mutate",
    "control.character.owner",
    "control.health.read",
    "control.operation.read",
    "identity.person.read",
    "identity.person.create",
    "identity.client.pair",
    "identity.client.revoke",
]


def private_write(path: Path, content: str | bytes) -> None:
    data = content.encode() if isinstance(content, str) else content
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(data)


@contextmanager
def locked(path: Path):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "a+") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def save_state(root: Path, state: dict[str, Any]) -> None:
    descriptor, name = tempfile.mkstemp(dir=root, prefix=".bootstrap-")
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(state, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, root / "bootstrap.json")
    finally:
        temporary.unlink(missing_ok=True)


def valid_slug(value: str) -> str:
    if re.fullmatch(r"[a-z][a-z0-9-]*", value) is None:
        raise ValueError("slug must match the character API: [a-z][a-z0-9-]*")
    return value


def public_url(value: str) -> str:
    url = urlsplit(value)
    if (
        url.scheme not in {"http", "https"}
        or not url.hostname
        or url.username
        or url.password
        or url.query
        or url.fragment
    ):
        raise ValueError(
            "public URL must be an http(s) base without credentials, query or fragment"
        )
    _ = url.port
    return value.rstrip("/")


def read_meta(base: str) -> dict[str, Any]:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(base.rstrip("/") + "/client/v1/meta", timeout=30) as response:
        return json.load(response)["data"]


def prepare(
    base: Path,
    slug: str,
    *,
    name: str,
    image: str,
    environment_image: str,
    port: int,
    url: str,
    persona: Path,
    system_prompt: Path,
    models: Path,
    owner_name: str = "本人",
) -> Path:
    valid_slug(slug)
    url = public_url(url)
    if not 1 <= port <= 65535:
        raise ValueError("invalid HTTP port")
    if not name.strip() or not image.strip() or not environment_image.strip():
        raise ValueError("name and both selected images are required")
    persona_bytes, system_bytes, model_bytes = (
        persona.read_bytes(),
        system_prompt.read_bytes(),
        models.read_bytes(),
    )
    if not persona_bytes.decode("utf-8").strip() or not system_bytes.decode("utf-8").strip():
        raise ValueError("both prompt documents must contain text")
    tomllib.loads(model_bytes.decode("utf-8"))
    base = base.resolve()
    root = base / slug
    if root.exists() or root.is_symlink():
        raise ValueError(
            "character directory already exists; resume it without preparing another instance"
        )
    for previous in base.glob("*/instance.json"):
        metadata = json.loads(previous.read_text())
        if metadata.get("http_port") == port:
            raise ValueError("another prepared character uses this HTTP port")
    root.mkdir(mode=0o700, parents=True)
    for directory in [
        "data",
        "workspace",
        "character",
        "config",
        "plugins",
        "social-transfer",
        "sandbox",
        "manager",
    ]:
        (root / directory).mkdir(mode=0o700)
    private_write(root / ".gitignore", "*\n")
    private_write(root / "character/persona.md", persona_bytes)
    private_write(root / "character/system_prompt.md", system_bytes)
    private_write(root / "config/model_profiles.toml", model_bytes)
    token = secrets.token_urlsafe(32)
    environment = {
        "YUKI_CHARACTER_SLUG": slug,
        "YUKI_BOT_IMAGE": image,
        "YUKI_HTTP_PORT": str(port),
        "BOT_DISPLAY_NAME": name,
        "BOT_ALIASES": name,
        "CLIENT_PUBLIC_URL": url,
        "YUKI_CONTROL_OPERATOR_TOKEN": token,
        "DESKTOP_SESSION_ENABLED": "true",
    }
    private_write(
        root / ".env",
        "".join(
            f"{key}={json.dumps(value, ensure_ascii=False)}\n" for key, value in environment.items()
        ),
    )
    operator = (
        f"[[operators]]\nprincipal_id={json.dumps(str(uuid4()))}\n"
        'token_env="YUKI_CONTROL_OPERATOR_TOKEN"\nenabled=true\nroles=["operator"]\n'
        f"capabilities={json.dumps(CAPABILITIES)}\n"
    )
    private_write(root / "config/control-operators.toml", operator)
    private_write(root / "compose.yaml", (HERE / "compose.yaml").read_bytes())
    manager = {
        "YUKI_MANAGER_ROOT": str(root / "sandbox"),
        "YUKI_MANAGER_HOME": str(root / "sandbox/home"),
        "YUKI_MANAGER_WORKSPACE_STORE": str(root / "workspace"),
        "YUKI_MANAGER_MEDIA_CACHE": str(root / "data/conversation-media-cache"),
        "YUKI_MANAGER_IMAGE": environment_image,
        "YUKI_MANAGER_DESKTOP_SESSION": "true",
    }
    private_write(
        root / "manager/deployment.env",
        "".join(
            f"{key}={json.dumps(value, ensure_ascii=False)}\n" for key, value in manager.items()
        ),
    )
    private_write(
        root / "manager/acp-agents.toml", "# Operator-selected ACP programs for this character.\n"
    )
    metadata = {
        "slug": slug,
        "display_name": name,
        "http_port": port,
        "public_url": url,
        "image": image,
        "environment_image": environment_image,
        "owner_name": owner_name,
        "mode": "managed",
        "project_root": str(root),
        "compose_project": f"yuki-{slug}",
        "compose_files": [str(root / "compose.yaml")],
        "env_file": str(root / ".env"),
        "manager_root": str(root / "sandbox"),
        "manager_home": str(root / "sandbox/home"),
        "environment_name": f"yuki-environment-{slug}",
        "manager_socket": f"/run/yuki-sandbox/{slug}/manager.sock",
        "upstream": f"http://127.0.0.1:{port}",
    }
    private_write(root / "instance.json", json.dumps(metadata, ensure_ascii=False, indent=2) + "\n")
    # This is a snippet for the operator's existing TLS server; installing it is explicit.
    from urllib.parse import urlsplit

    prefix = urlsplit(url).path.rstrip("/") + "/"
    private_write(
        root / "public-proxy.conf",
        f"location ^~ {json.dumps(prefix)} {{\n"
        f"    proxy_pass http://127.0.0.1:{port}/;\n"
        "    proxy_http_version 1.1;\n"
        "    proxy_buffering off;\n"
        "    proxy_set_header Upgrade $http_upgrade;\n"
        '    proxy_set_header Connection "upgrade";\n'
        "}\n",
    )
    save_state(root, {})
    return root


def command(root: Path, *arguments: str, environment: dict[str, str] | None = None) -> str:
    result = subprocess.run(
        list(arguments), cwd=root, check=True, capture_output=True, text=True, env=environment
    )
    return result.stdout


def compose(root: Path, *arguments: str, environment: dict[str, str] | None = None) -> str:
    metadata = json.loads((root / "instance.json").read_text())
    selected = (
        {
            "YUKI_CHARACTER_SLUG": metadata["slug"],
            "YUKI_BOT_IMAGE": metadata["image"],
            "YUKI_HTTP_PORT": str(metadata["http_port"]),
        }
        if metadata.get("mode") == "managed"
        else {}
    )
    environment = {**(os.environ if environment is None else environment), **selected}
    files = tuple(part for path in metadata["compose_files"] for part in ("--file", path))
    return command(
        Path(metadata["project_root"]),
        "docker",
        "compose",
        "--project-name",
        metadata["compose_project"],
        *files,
        "--env-file",
        metadata["env_file"],
        *arguments,
        environment=environment,
    )


def admin(root: Path, *arguments: str) -> Any:
    # Pass the existing operator secret via Docker's inherited environment,
    # never as a process argument, a state field or a printed command.
    token = None
    metadata = json.loads((root / "instance.json").read_text())
    for line in Path(metadata["env_file"]).read_text().splitlines():
        key, separator, value = line.partition("=")
        if separator and key == "YUKI_CONTROL_OPERATOR_TOKEN":
            words = shlex.split(value, comments=True)
            token = words[0] if len(words) == 1 else None
    if not token:
        raise ValueError("operator credential is missing from this instance")
    environment = {**os.environ, "YUKI_CONTROL_CREDENTIAL": token}
    return json.loads(
        compose(
            root,
            "exec",
            "-T",
            "--user",
            "10001:10001",
            "--env",
            "YUKI_CONTROL_CREDENTIAL",
            "bot",
            "yuki",
            "admin",
            *arguments,
            environment=environment,
        )
    )


def bootstrap(root: Path, *, issue_pair: bool = False) -> str | None:
    with locked(root / ".bootstrap.lock"):
        return _bootstrap(root, issue_pair=issue_pair)


def _bootstrap(root: Path, *, issue_pair: bool = False) -> str | None:
    metadata = json.loads((root / "instance.json").read_text())
    state = json.loads((root / "bootstrap.json").read_text())

    def request(key: str) -> str:
        if key not in state:
            state[key] = str(uuid4())
            save_state(root, state)
        return str(state[key])

    if not state.get("character_ready"):
        if "character_revision" not in state:
            current = admin(root, "character", "show")
            state["character_revision"] = current["revision"]
            save_state(root, state)
        result = admin(
            root,
            "character",
            "update",
            "--display-name",
            metadata["display_name"],
            "--slug",
            metadata["slug"],
            "--revision",
            str(state["character_revision"]),
            "--request-id",
            request("character_request"),
        )
        if not isinstance(result.get("id"), str):
            raise ValueError("character update did not return the original character")
        state["character_id"] = result["id"]
        state["character_ready"] = True
        save_state(root, state)
    public = read_meta(metadata["public_url"])
    if public["character"]["id"] != state["character_id"]:
        raise ValueError("public pairing URL identifies a different character")
    if "owner_person_id" not in state:
        created = admin(
            root,
            "person",
            "create",
            "--display-name",
            metadata["owner_name"],
            "--request-id",
            request("person_request"),
        )
        if created.get("success") is not True:
            raise ValueError("owner Person creation is not confirmed")
        state["owner_person_id"] = created["resource_id"]
        save_state(root, state)
    if not state.get("owner_ready"):
        owner = admin(
            root,
            "character",
            "owner",
            "grant",
            "--person",
            state["owner_person_id"],
            "--request-id",
            request("owner_request"),
        )
        if owner.get("granted") is not True:
            raise ValueError("owner grant is not confirmed")
        state["owner_ready"] = True
        save_state(root, state)
    if issue_pair and state.get("pair_issued"):
        state.pop("pair_request", None)
        state["pair_issued"] = False
        save_state(root, state)
    if not state.get("pair_issued"):
        paired = admin(
            root,
            "client",
            "pair",
            "--person",
            state["owner_person_id"],
            "--request-id",
            request("pair_request"),
        )
        state["pair_issued"] = True
        state["pair_code_id"] = paired["code_id"]
        state["pair_expires_at"] = paired["expires_at"]
        save_state(root, state)
        if paired.get("code"):
            return "yuki://pair?" + urlencode(
                {"server": metadata["public_url"], "code": paired["code"]}
            )
    return None


def install_file(source: Path, destination: Path) -> None:
    if destination.exists() or destination.is_symlink():
        if destination.is_symlink() or destination.read_bytes() != source.read_bytes():
            raise ValueError(f"existing operator file differs; preserve and review {destination}")
        return
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    private_write(destination, source.read_bytes())
    destination.chmod(0o644 if destination.suffix == ".service" else 0o600)


def start(root: Path, *, issue_pair: bool = False) -> str | None:
    if os.geteuid() != 0:
        raise ValueError("starting host systemd managers and storage requires root")
    metadata = json.loads((root / "instance.json").read_text())
    slug = valid_slug(metadata["slug"])
    if metadata.get("mode") != "managed":
        raise ValueError("preserve the adopted instance; start it through its original deployment")
    # Do not implicitly pull or substitute an image selected by the operator.
    command(root, "docker", "image", "inspect", metadata["image"])
    command(root, "docker", "image", "inspect", metadata["environment_image"])
    for relative in ["data", "workspace", "character", "config", "plugins", "social-transfer"]:
        directory = root / relative
        directory.chmod(0o750)
        os.chown(directory, 10001, 10001)
    cache = root / "data/conversation-media-cache"
    cache.mkdir(mode=0o750, exist_ok=True)
    os.chown(cache, 10001, 10001)
    for relative in [
        "character/persona.md",
        "character/system_prompt.md",
        "config/model_profiles.toml",
        "config/control-operators.toml",
    ]:
        os.chown(root / relative, 10001, 10001)
    for filename in ["yuki-sandbox@.service", "yuki-environment-storage@.service"]:
        install_file(HERE / filename, Path("/etc/systemd/system") / filename)
    policy = Path("/etc/yuki-sandbox") / slug
    install_file(root / "manager/deployment.env", policy / "deployment.env")
    # A registered peer list belongs to the operator. Never replace it with an empty template.
    if not (policy / "acp-agents.toml").exists():
        install_file(root / "manager/acp-agents.toml", policy / "acp-agents.toml")
    command(root, "systemctl", "daemon-reload")
    command(root, "systemctl", "enable", "--now", f"yuki-sandbox@{slug}.service")
    compose(root, "config", "--quiet")
    compose(root, "up", "-d", "--wait", "bot")
    return bootstrap(root, issue_pair=issue_pair)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("slug", type=valid_slug)
    parser.add_argument("--root", type=Path, default=Path("/srv/yuki-characters"))
    parser.add_argument("--name")
    parser.add_argument("--image")
    parser.add_argument("--environment-image")
    parser.add_argument("--port", type=int)
    parser.add_argument("--public-url")
    parser.add_argument("--persona", type=Path)
    parser.add_argument("--system-prompt", type=Path)
    parser.add_argument("--models", type=Path)
    parser.add_argument("--owner-name", default="本人")
    parser.add_argument("--start", action="store_true")
    parser.add_argument("--pair-code", action="store_true")
    args = parser.parse_args()
    if args.start or args.pair_code:
        root = args.root.resolve() / args.slug
        if not (root / "instance.json").is_file():
            parser.error("prepare this independent character before starting it")
        if args.start:
            link = start(root, issue_pair=args.pair_code)
        else:
            state = json.loads((root / "bootstrap.json").read_text())
            if not state.get("owner_ready"):
                parser.error("start and initialize this instance before requesting another code")
            link = bootstrap(root, issue_pair=True)
        print(link or "Code was already issued. Use --pair-code to explicitly issue a new code.")
        return
    values = [
        args.name,
        args.image,
        args.environment_image,
        args.port,
        args.public_url,
        args.persona,
        args.system_prompt,
        args.models,
    ]
    if any(value is None for value in values):
        parser.error(
            "preparation requires --name, both images, --port, --public-url, "
            "both prompts and --models"
        )
    root = prepare(
        args.root,
        args.slug,
        name=args.name,
        image=args.image,
        environment_image=args.environment_image,
        port=args.port,
        url=args.public_url,
        persona=args.persona,
        system_prompt=args.system_prompt,
        models=args.models,
        owner_name=args.owner_name,
    )
    print(f"Prepared {root}; no service or model was started.")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError) as failure:
        raise SystemExit(str(failure)) from None
    except subprocess.CalledProcessError as failure:
        raise SystemExit(
            f"Command failed with status {failure.returncode}; "
            "original bootstrap request IDs are preserved."
        ) from None
