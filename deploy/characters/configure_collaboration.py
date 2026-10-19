"""Prepare or explicitly apply same-host ACP collaboration to registered instances."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import subprocess
import tempfile
from pathlib import Path
from uuid import uuid4

from collaboration import (
    append_program,
    instance,
    peer_program,
    program_text,
    refresh_firewall,
    relay_config,
    source_addresses,
)
from new_character import admin, locked, private_write, read_meta, valid_slug

ROOT = Path(__file__).parent


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(dir=path.parent, prefix=".state-")
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def owned_file(path: Path, content: bytes, receipts: Path) -> None:
    """Replace only bytes previously installed by this tool; preserve operator edits."""
    with locked(receipts.with_suffix(".lock")):
        state = json.loads(receipts.read_text()) if receipts.exists() else {}
        digest = hashlib.sha256(content).hexdigest()
        if path.is_symlink():
            raise ValueError(f"preserve operator symlink: {path}")
        if path.exists():
            current = hashlib.sha256(path.read_bytes()).hexdigest()
            if current != digest and state.get(str(path)) != current:
                raise ValueError(f"preserve modified operator file: {path}")
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(dir=path.parent, prefix=".install-")
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, path)
        finally:
            Path(name).unlink(missing_ok=True)
        state[str(path)] = digest
        atomic_json(receipts, state)


def identity(root: Path, metadata: dict) -> str:
    expected = metadata.get("character_id")
    if not expected:
        expected = json.loads((root / "bootstrap.json").read_text())["character_id"]
    for url in (metadata["upstream"], metadata["public_url"]):
        if read_meta(url)["character"]["id"] != expected:
            raise ValueError("registered URL now identifies a different character")
    return expected


def execute(
    caller: dict, *arguments: str, environment: dict | None = None, input_text: str | None = None
) -> str:
    inherited = os.environ if environment is None else environment
    passed = tuple(
        part for key in ("YUKI_PEER_CODE",) if key in inherited for part in ("--env", key)
    )
    result = subprocess.run(
        ["docker", "exec", "-i", "--user", "0:0", *passed, caller["environment_name"], *arguments],
        input=input_text,
        env=inherited,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def container_meta(caller: dict, url: str) -> dict:
    code = (
        "import json,urllib.request; "
        "op=urllib.request.build_opener(urllib.request.ProxyHandler({})); "
        "print(op.open("
        + repr(url.rstrip("/") + "/client/v1/meta")
        + ",timeout=30).read().decode())"
    )
    return json.loads(execute(caller, "/opt/yuki-client/bin/python", "-I", "-c", code))["data"]


def existing_credential(caller: dict, callee: str, base: str) -> dict | None:
    token = f"/home/yuki/.config/yuki-peers/{callee}/token"
    state = f"/home/yuki/.yuki-peers/{callee}/outbox.sqlite3"
    script = (
        f"test -f {token} || exit 44; "
        f'YUKI_CLIENT_TOKEN="$(cat {token})" exec /opt/yuki-client/bin/yuki '
        f"client --url {base} --state {state} me"
    )
    try:
        return json.loads(execute(caller, "/bin/sh", "-c", script))
    except subprocess.CalledProcessError as failure:
        if failure.returncode == 44:
            return None
        raise


def store_token(caller: dict, slug: str, token: str, *, replace: bool) -> None:
    # Open model-writable paths only INSIDE the original environment, never on the host.
    code = """import json,os,pathlib,sys,tempfile
v=json.load(sys.stdin)
p=pathlib.Path('/home/yuki/.config/yuki-peers')/v['slug']/'token'
p.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
s=pathlib.Path('/home/yuki/.yuki-peers')/v['slug']
s.mkdir(mode=0o700,parents=True,exist_ok=True)
fd,name=tempfile.mkstemp(dir=p.parent,prefix='.token-')
try:
 with os.fdopen(fd,'w') as f:
  f.write(v['token']); f.flush(); os.fsync(f.fileno())
 if v['replace']: os.replace(name,p)
 else: os.link(name,p)
finally:
 pathlib.Path(name).unlink(missing_ok=True)
"""
    execute(
        caller,
        "/opt/yuki-client/bin/python",
        "-I",
        "-c",
        code,
        input_text=json.dumps({"slug": slug, "token": token, "replace": replace}),
    )


def connect_peer(
    registry: Path,
    caller: dict,
    callee: dict,
    gateway: str,
    port: int,
    *,
    replace_credential: bool = False,
) -> dict:
    caller_root, callee_root = registry / caller["slug"], registry / callee["slug"]
    caller_id, callee_id = identity(caller_root, caller), identity(callee_root, callee)
    if caller_id == callee_id:
        raise ValueError("collaboration requires two independent character identities")
    base = f"http://{gateway}:{port}/{callee['slug']}"
    if container_meta(caller, base)["character"]["id"] != callee_id:
        raise ValueError("environment relay identifies a different character")
    path = caller_root / "manager/peers" / (callee["slug"] + ".json")
    with locked(path.with_suffix(".lock")):
        state = json.loads(path.read_text()) if path.exists() else {}
        identities = {"caller_character_id": caller_id, "callee_character_id": callee_id}
        if state and any(state.get(key) != value for key, value in identities.items()):
            raise ValueError("preserve peer state belonging to another character identity")
        state.update(identities)

        def request(key: str) -> str:
            if key not in state:
                state[key] = str(uuid4())
                atomic_json(path, state)
            return state[key]

        me = existing_credential(caller, callee["slug"], base)
        if me is not None and not replace_credential:
            if me["request_person_id"] != state.get("person_id"):
                raise ValueError("existing peer token belongs to another Person")
            state["connected"] = True
            atomic_json(path, state)
            return state
        if state.get("pair_issued") and not replace_credential:
            raise ValueError(
                "pair code already issued, credential missing; inspect original device "
                "and explicitly use --replace-credential to issue a new code"
            )
        if "person_id" not in state:
            created = admin(
                callee_root,
                "person",
                "create",
                "--display-name",
                caller["display_name"],
                "--request-id",
                request("person_request"),
            )
            if created.get("success") is not True:
                raise ValueError("ordinary peer Person creation is not confirmed")
            state["person_id"] = created["resource_id"]
            atomic_json(path, state)
        if replace_credential and state.get("device_id"):
            revoked = admin(
                callee_root,
                "client",
                "revoke",
                state["device_id"],
                "--request-id",
                request("revoke_request"),
            )
            if revoked.get("revoked") is not True:
                raise ValueError("original peer device revocation is not confirmed")
            state.pop("device_id", None)
        if replace_credential:
            state.pop("pair_request", None)
            state.pop("revoke_request", None)
        paired = admin(
            callee_root,
            "client",
            "pair",
            "--person",
            state["person_id"],
            "--request-id",
            request("pair_request"),
        )
        state.update(pair_issued=True, code_id=paired["code_id"], expires_at=paired["expires_at"])
        atomic_json(path, state)
        if not paired.get("code"):
            raise ValueError("original code cannot be redisplayed; explicitly issue a new code")
        result = json.loads(
            execute(
                caller,
                "/opt/yuki-client/bin/yuki",
                "client",
                "--url",
                base,
                "--state",
                f"/home/yuki/.yuki-peers/{callee['slug']}/outbox.sqlite3",
                "pair",
                "--code-env",
                "YUKI_PEER_CODE",
                "--label",
                f"{caller['display_name']} ACP",
                environment={**os.environ, "YUKI_PEER_CODE": paired["code"]},
            )
        )
        state["device_id"] = result["device_id"]
        atomic_json(path, state)
        store_token(caller, callee["slug"], result["token"], replace=replace_credential)
        me = existing_credential(caller, callee["slug"], base)
        if me is None or me["request_person_id"] != state["person_id"]:
            raise ValueError("new peer credential did not resolve to its original Person")
        state["connected"] = True
        atomic_json(path, state)
        return state


def prepare(
    registry: Path, callers: list[str], callees: list[str], gateway: str, port: int, output: Path
) -> dict:
    sources = [instance(registry, slug) for slug in callers]
    destinations = [instance(registry, slug) for slug in callees]
    configuration = {"gateway": gateway, "port": port, "callers": sources, "callees": destinations}
    relay = relay_config(destinations, gateway, port)
    # Exclusive creation keeps preparation from overwriting previous review material.
    output.mkdir(mode=0o700, parents=True)
    private_write(output / "relay.conf", relay)
    private_write(output / "collaboration.json", json.dumps(configuration, indent=2) + "\n")
    for caller in sources:
        for callee in destinations:
            if caller["slug"] != callee["slug"]:
                private_write(
                    output / f"{caller['slug']}-to-{callee['slug']}.toml",
                    program_text(callee["slug"], peer_program(callee, gateway, port)),
                )
    return configuration


def apply(
    registry: Path,
    prepared: Path,
    relay_destination: Path,
    *,
    existing_service: str | None = None,
    existing_acp: Path | None = None,
    replace_credential: bool = False,
) -> None:
    if os.geteuid() != 0:
        raise ValueError("host collaboration installation requires root")
    config = json.loads((prepared / "collaboration.json").read_text())
    # Resolve registry again: review files cannot supply an alternate HOME or secret path.
    config["callers"] = [instance(registry, item["slug"]) for item in config["callers"]]
    config["callees"] = [instance(registry, item["slug"]) for item in config["callees"]]
    for item in config["callers"] + config["callees"]:
        identity(registry / item["slug"], item)
    addresses = source_addresses(config)
    if len(addresses) != len({item["environment_name"] for item in config["callers"]}):
        raise ValueError("start all original caller environments before applying collaboration")
    receipts = registry / ".collaboration-files.json"
    relay = relay_config(config["callees"], config["gateway"], config["port"]).encode()
    if relay != (prepared / "relay.conf").read_bytes():
        raise ValueError("registry changed; prepare and review the relay again")
    owned_file(relay_destination, relay, receipts)
    subprocess.run(["nginx", "-t"], check=True)
    subprocess.run(["systemctl", "reload", "nginx"], check=True)
    config_path = Path("/etc/yuki-sandbox/collaboration.json")
    owned_file(config_path, (json.dumps(config, indent=2) + "\n").encode(), receipts)
    hook = (
        "#!/bin/sh\nset -eu\n/opt/yuki-sandbox/venv/bin/python "
        "/opt/yuki-sandbox/deploy/characters/collaboration.py "
        "--firewall /etc/yuki-sandbox/collaboration.json\n"
    )
    hook_path = Path("/etc/yuki-sandbox/collaboration-firewall.sh")
    owned_file(hook_path, hook.encode(), receipts)
    hook_path.chmod(0o700)
    owned_file(
        Path("/etc/systemd/system/yuki-collaboration-firewall.service"),
        (ROOT / "yuki-collaboration-firewall.service").read_bytes(),
        receipts,
    )
    refresh_firewall(config_path)
    for caller in config["callers"]:
        if caller["mode"] == "existing":
            if not existing_service or not existing_acp or not existing_acp.is_absolute():
                raise ValueError("supply the original Manager service and trusted ACP config path")
            service, acp = existing_service, existing_acp
            if "/" in service or not service.endswith(".service"):
                raise ValueError("invalid original Manager unit")
            launch = subprocess.run(
                ["systemctl", "show", "--property=ExecStart", "--value", service],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            if "--acp-config" in launch and str(acp) not in launch:
                raise ValueError("original Manager has another explicit ACP config; preserve it")
            dropin = Path("/etc/systemd/system") / (service + ".d") / "50-yuki-collaboration.conf"
            owned_file(
                dropin,
                (
                    "[Service]\nEnvironment="
                    + shlex.quote("YUKI_MANAGER_ACP_CONFIG=" + str(acp))
                    + "\n"
                ).encode(),
                receipts,
            )
        else:
            service = f"yuki-sandbox@{caller['slug']}.service"
            acp = Path("/etc/yuki-sandbox") / caller["slug"] / "acp-agents.toml"
        for callee in config["callees"]:
            if caller["slug"] == callee["slug"]:
                continue
            connect_peer(
                registry,
                caller,
                callee,
                config["gateway"],
                config["port"],
                replace_credential=replace_credential,
            )
            append_program(
                acp, callee["slug"], peer_program(callee, config["gateway"], config["port"])
            )
        subprocess.run(["systemctl", "daemon-reload"], check=True)
        subprocess.run(["systemctl", "restart", service], check=True)
    subprocess.run(["systemctl", "daemon-reload"], check=True)
    subprocess.run(
        ["systemctl", "enable", "--now", "yuki-collaboration-firewall.service"], check=True
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", type=Path, default=Path("/srv/yuki-characters"))
    commands = parser.add_subparsers(dest="action", required=True)
    create = commands.add_parser("prepare")
    create.add_argument("--caller", type=valid_slug, action="append", required=True)
    create.add_argument("--callee", type=valid_slug, action="append", required=True)
    create.add_argument("--gateway", required=True)
    create.add_argument("--port", type=int, default=18780)
    create.add_argument("--output", type=Path, required=True)
    install = commands.add_parser("apply")
    install.add_argument("prepared", type=Path)
    install.add_argument("--relay-destination", type=Path, required=True)
    install.add_argument("--existing-service")
    install.add_argument("--existing-acp", type=Path)
    install.add_argument("--replace-credential", action="store_true")
    args = parser.parse_args()
    if args.action == "prepare":
        prepare(args.registry, args.caller, args.callee, args.gateway, args.port, args.output)
        print(f"Prepared collaboration in {args.output}; review before explicit apply.")
    else:
        apply(
            args.registry,
            args.prepared,
            args.relay_destination,
            existing_service=args.existing_service,
            existing_acp=args.existing_acp,
            replace_credential=args.replace_credential,
        )
        print("Installed ordinary peer credentials and trusted ACP programs.")


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as failure:
        raise SystemExit(
            f"Original operation failed (exit {failure.returncode}); "
            "saved request IDs remain available for recovery."
        ) from None
    except (ValueError, OSError, KeyError) as failure:
        raise SystemExit(str(failure)) from None
