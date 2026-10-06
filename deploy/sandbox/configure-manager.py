"""Select the existing artifact store without moving home or overwriting configuration."""

from __future__ import annotations

import argparse
import os
import tempfile
from pathlib import Path

KEY = "YUKI_MANAGER_WORKSPACE_STORE"
DEFAULT_STORE = Path("/opt/yuki-qqbot/workspace")


def _store_value(value: str) -> str:
    """Accept our whole quoted value or a simple literal path, never shell grammar."""
    value = value.strip()
    if not value or any(character in value for character in "\n\r\x00"):
        raise ValueError("unsupported_manager_workspace_value")
    if not value.startswith('"'):
        if any(character.isspace() or character in "'\"\\" for character in value):
            raise ValueError("unsupported_manager_workspace_value")
        return value  # EnvironmentFile does not treat inline # as a comment.
    if len(value) < 2 or not value.endswith('"'):
        raise ValueError("unsupported_manager_workspace_value")
    body, decoded, index = value[1:-1], [], 0
    while index < len(body):
        character = body[index]
        if character == "\\":
            index += 1
            if index == len(body) or body[index] not in {'"', "\\"}:
                raise ValueError("unsupported_manager_workspace_value")
            character = body[index]
        elif character == '"':
            raise ValueError("unsupported_manager_workspace_value")
        decoded.append(character)
        index += 1
    return "".join(decoded)


def configure_store(environment: Path, deployment_root: Path | None, *, check: bool) -> Path:
    if environment.is_symlink() or (environment.exists() and not environment.is_file()):
        raise ValueError("unsafe_manager_environment_file")
    original = b""
    if environment.exists():
        with environment.open("rb") as stream:
            original = stream.read(16_385)
    if len(original) > 16_384:
        raise ValueError("manager_environment_file_too_large")
    content = original.decode("utf-8")
    configured = None
    for line in content.splitlines():
        if (len(line) - len(line.rstrip("\\"))) % 2:
            raise ValueError("unsupported_manager_environment_continuation")
        name, separator, value = line.partition("=")
        if separator and name.strip() == KEY:
            if configured is not None:
                raise ValueError("invalid_manager_workspace_configuration")
            configured = Path(_store_value(value))
    if configured is not None and not configured.is_absolute():
        raise ValueError("manager_workspace_store_must_be_absolute")
    if deployment_root is None:
        return configured or DEFAULT_STORE
    if (
        not deployment_root.is_absolute()
        or deployment_root.is_symlink()
        or not deployment_root.is_dir()
    ):
        raise ValueError("deployment_root_must_be_an_existing_absolute_directory")
    desired = deployment_root.resolve() / "workspace"
    if any(character in str(desired) for character in "\n\r\x00"):
        raise ValueError("invalid_deployment_root")
    if configured is not None:
        if configured != desired:
            raise ValueError("manager_workspace_already_configured_use_existing_deployment")
        return configured
    if check:
        return desired
    # Only append this installation's independent setting. Never shell-source or
    # replace existing EnvironmentFile values, including custom runtime settings.
    encoded = str(desired).replace("\\", "\\\\").replace('"', '\\"')
    updated = original + (b"\n" if original and not original.endswith(b"\n") else b"")
    updated += f'{KEY}="{encoded}"\n'.encode()
    environment.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(dir=environment.parent, prefix=".deployment-")
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(updated)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(0o600)
        os.replace(temporary, environment)
    finally:
        temporary.unlink(missing_ok=True)
    return desired


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--environment-file", type=Path, default=Path("/etc/yuki-sandbox/deployment.env")
    )
    parser.add_argument("--deployment-root", type=Path)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    print(configure_store(args.environment_file, args.deployment_root, check=args.check))


if __name__ == "__main__":
    main()
