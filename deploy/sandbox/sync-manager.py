"""Export Manager code from the selected Bot; preserve host data and service arguments."""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import subprocess
import tempfile
from pathlib import Path

_ENVIRONMENT_WORD = re.compile(r"""(?:[^\s"'\\]|\\.|"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')+""")


def command(*arguments: str) -> str:
    return subprocess.check_output(arguments, text=True).strip()


def bot_identity(container: str) -> tuple[str, str]:
    values = command(
        "docker",
        "inspect",
        "--format",
        '{{.Image}}\n{{index .Config.Labels "org.opencontainers.image.revision"}}',
        container,
    ).splitlines()
    image, revision = values
    if re.fullmatch(r"[0-9a-f]{40}", revision) is None:
        raise ValueError("Bot image must declare its full source revision")
    return image, revision


def source_files(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"
    }


def quote_environment(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def source_override(original: str, source: Path) -> str:
    lines = original.splitlines(keepends=True)
    output: list[str] = []
    section = ""
    service_end: int | None = None
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            if section == "Service":
                service_end = len(output)
            section = stripped[1:-1]
        if section == "Service" and stripped.startswith("Environment="):
            prefix, _, value = line.partition("=")
            spans = [
                word.span()
                for word in _ENVIRONMENT_WORD.finditer(value)
                if re.match(r"""["']?PYTHONPATH=""", word.group())
            ]
            if spans:
                retained = value
                for start, end in reversed(spans):
                    retained = retained[:start] + retained[end:]
                if retained.strip():
                    output.append(prefix + "=" + retained)
                continue
        output.append(line)
    if section == "Service":
        service_end = len(output)
    setting = "Environment=" + quote_environment(f"PYTHONPATH={source}".replace("%", "%%"))
    if service_end is None:
        if output and not output[-1].endswith("\n"):
            output[-1] += "\n"
        output.extend(["[Service]\n", setting + "\n"])
    else:
        if service_end and not output[service_end - 1].endswith("\n"):
            output[service_end - 1] += "\n"
        output.insert(service_end, setting + "\n")
    return "".join(output)


def sync_manager(container: str, manager_root: Path, override: Path) -> dict[str, str]:
    manager_root = manager_root.resolve()
    override = override.resolve()
    image, revision = bot_identity(container)
    package = command(
        "docker",
        "exec",
        container,
        "python",
        "-c",
        "from pathlib import Path; import yuki; print(Path(yuki.__file__).resolve().parent)",
    )
    release = manager_root / "releases" / revision
    source = release / "src"
    original = override.read_text(encoding="utf-8") if override.exists() else ""
    updated = source_override(original, source)
    release.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".manager-export-", dir=release.parent) as temporary:
        staged = Path(temporary) / "release"
        staged_package = staged / "src" / "yuki"
        staged_package.mkdir(parents=True)
        subprocess.run(
            ["docker", "cp", f"{container}:{package}/.", str(staged_package)], check=True
        )
        if bot_identity(container) != (image, revision):
            raise RuntimeError("Bot changed while exporting Manager source")
        if release.exists():
            if source_files(source / "yuki") != source_files(staged_package):
                raise ValueError("Existing release differs from the selected Bot; keep it intact")
        else:
            staged.rename(release)
    # Published code is read by the Bot UID as well as the root Manager.
    # These directories are created here, so their public traversal cannot
    # depend on the installer's private umask. Keep staged exports private.
    for directory in (release.parent, release, source, source / "yuki"):
        directory.chmod(0o755)
    override.parent.mkdir(parents=True, exist_ok=True)
    mode = stat.S_IMODE(override.stat().st_mode) if override.exists() else 0o644
    descriptor, name = tempfile.mkstemp(dir=override.parent, prefix=".manager-source-")
    temporary_override = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(updated)
            stream.flush()
            os.fsync(stream.fileno())
        temporary_override.chmod(mode)
        os.replace(temporary_override, override)
    finally:
        temporary_override.unlink(missing_ok=True)
    return {"image": image, "revision": revision, "source": str(source), "override": str(override)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bot-container", required=True)
    parser.add_argument("--manager-root", type=Path, default=Path("/opt/yuki-sandbox"))
    parser.add_argument(
        "--source-override",
        type=Path,
        default=Path("/etc/systemd/system/yuki-sandbox.service.d/90-source-release.conf"),
    )
    args = parser.parse_args()
    print(
        json.dumps(
            sync_manager(args.bot_container, args.manager_root, args.source_override),
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
