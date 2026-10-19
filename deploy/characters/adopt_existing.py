"""Register references to an existing character; never copy or initialize its data."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from uuid import UUID

from new_character import private_write, public_url, read_meta, valid_slug


def adopt(
    registry: Path,
    slug: str,
    *,
    project_root: Path,
    compose_project: str,
    compose_files: list[Path],
    env_file: Path,
    manager_root: Path,
    home: Path,
    environment_name: str,
    socket: Path,
    upstream: str,
    url: str,
) -> Path:
    slug = valid_slug(slug)
    url, upstream = public_url(url), public_url(upstream)
    project_root, registry = project_root.resolve(), registry.resolve()
    root = registry / slug
    if root.is_relative_to(project_root):
        raise ValueError("keep the registry outside the existing deployment")
    if root.exists() or root.is_symlink():
        raise ValueError("registry entry already exists; original deployment is unchanged")
    if not project_root.is_dir() or not home.is_dir() or not manager_root.is_dir():
        raise ValueError("the original deployment, HOME and Manager root must already exist")
    if (
        not env_file.is_file()
        or not compose_files
        or any(not item.is_file() for item in compose_files)
    ):
        raise ValueError("record the original environment file and complete Compose chain")
    if (
        not compose_project
        or re.fullmatch(r"yuki-environment(?:-[a-z0-9-]+)?", environment_name) is None
    ):
        raise ValueError("invalid original Compose project or environment name")
    if not socket.is_absolute():
        raise ValueError("the original Manager socket path must be absolute")
    current = read_meta(url)
    character = current["character"]
    identity = str(UUID(character["id"]))
    # Verify the private relay upstream before any future credential can use it.
    if read_meta(upstream)["character"]["id"] != identity:
        raise ValueError("public URL and private upstream identify different characters")
    root.mkdir(mode=0o700, parents=True)
    metadata = {
        "mode": "existing",
        "slug": slug,
        "character_id": identity,
        "display_name": character["display_name"],
        "public_url": url,
        "project_root": str(project_root),
        "compose_project": compose_project,
        "compose_files": [str(item.resolve()) for item in compose_files],
        "env_file": str(env_file.resolve()),
        "manager_root": str(manager_root.resolve()),
        "manager_home": str(home.resolve()),
        "environment_name": environment_name,
        "manager_socket": str(socket),
        "upstream": upstream,
        "server_version": current["server_version"],
        "api_revision": current["api_revision"],
    }
    private_write(root / ".gitignore", "*\n")
    private_write(root / "instance.json", json.dumps(metadata, ensure_ascii=False, indent=2) + "\n")
    return root


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("slug", type=valid_slug)
    parser.add_argument("--registry", type=Path, default=Path("/srv/yuki-characters"))
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--compose-project", required=True)
    parser.add_argument("--compose-file", type=Path, action="append", required=True)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--manager-root", type=Path, required=True)
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--environment-name", default="yuki-environment")
    parser.add_argument("--socket", type=Path, required=True)
    parser.add_argument("--upstream", required=True)
    parser.add_argument("--public-url", required=True)
    args = parser.parse_args()
    root = adopt(
        args.registry,
        args.slug,
        project_root=args.project_root,
        compose_project=args.compose_project,
        compose_files=args.compose_file,
        env_file=args.env_file,
        manager_root=args.manager_root,
        home=args.home,
        environment_name=args.environment_name,
        socket=args.socket,
        upstream=args.upstream,
        url=args.public_url,
    )
    print(f"Registered {root}; original services, identity, history and HOME were not changed.")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, KeyError) as failure:
        raise SystemExit(str(failure)) from None
