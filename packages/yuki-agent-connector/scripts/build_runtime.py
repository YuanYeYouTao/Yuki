"""Build a relocatable product runtime. Never runs on an end user's installation."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import urllib.request
from pathlib import Path

PYTHON_RELEASE = "20261021"
PYTHON_VERSION = "3.12.15"
TARGETS = {
    ("win32", "AMD64"): (
        "win32",
        "x64",
        "x86_64-pc-windows-msvc",
        "aac5d331fc3307343f95928fdbea4583f0d53973d08c7e1d081884fbd229a0e1",
    ),
    ("darwin", "arm64"): (
        "darwin",
        "arm64",
        "aarch64-apple-darwin",
        "7b8d00b751db21cea87dc551a8f8165147c5fbc5251d5537d7e5497dde00a219",
    ),
    ("darwin", "x86_64"): (
        "darwin",
        "x64",
        "x86_64-apple-darwin",
        "f12e0504397683d6be0745ffd4c39ef45a455cec169adf35c628840d06133773",
    ),
    ("linux", "x86_64"): (
        "linux",
        "x64",
        "x86_64-unknown-linux-gnu",
        "6d7521138fd33afddc1ccd08abb007d9a2829e38117c2576f9efdc0ec73ce772",
    ),
    ("linux", "aarch64"): (
        "linux",
        "arm64",
        "aarch64-unknown-linux-gnu",
        "b4567aa5a11f51b540c4dfd9e082ebf23e554f8ac17b85caa6cea23e83693394",
    ),
}


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cache", type=Path, required=True)
    args = parser.parse_args()
    target_os, arch, triple, expected = TARGETS[sys.platform, platform.machine()]
    root = Path(__file__).resolve().parents[1]
    output, cache = args.output.resolve(), args.cache.resolve()
    if output.exists():
        raise FileExistsError(f"Choose a new build output directory: {output}")
    cache.mkdir(parents=True, exist_ok=True)
    name = f"cpython-{PYTHON_VERSION}+{PYTHON_RELEASE}-{triple}-install_only_stripped.tar.gz"
    archive = cache / name
    url = f"https://github.com/astral-sh/python-build-standalone/releases/download/{PYTHON_RELEASE}/{name}"
    if not archive.exists():
        urllib.request.urlretrieve(url, archive)
    if digest(archive) != expected:
        raise ValueError("The pinned Python archive digest does not match")
    output.mkdir(parents=True)
    with tarfile.open(archive) as bundle:
        bundle.extractall(output, filter="data")
    python = output / "python" / ("python.exe" if target_os == "win32" else "bin/python3")
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"}
    }
    env["PYTHONDONTWRITEBYTECODE"] = "1"

    def run(*command: str, cwd: Path = root) -> None:
        subprocess.run(command, check=True, cwd=cwd, env=env)

    with tempfile.TemporaryDirectory(prefix="yuki-connector-build-") as temp:
        temporary = Path(temp)
        requirements = temporary / "requirements.txt"
        run(
            "uv",
            "export",
            "--quiet",
            "--frozen",
            "--no-dev",
            "--no-emit-project",
            "--output-file",
            str(requirements),
        )
        run(
            "uv",
            "pip",
            "install",
            "--python",
            str(python),
            "--require-hashes",
            "-r",
            str(requirements),
        )
        run("uv", "build", "--wheel", "--out-dir", str(temporary))
        wheel = next(temporary.glob("*.whl"))
        run("uv", "pip", "install", "--python", str(python), "--no-deps", str(wheel))
    # Relocate before probing; no development venv or source import path is available.
    relocated = output.with_name(output.name + "-relocation-中文")
    output.rename(relocated)
    moved_python = relocated / python.relative_to(output)
    try:
        run(str(moved_python), "-I", "-B", "-m", "yuki_agent_connector", "--version", cwd=relocated)
        run(
            str(moved_python),
            "-I",
            "-B",
            "-c",
            "import ssl,sqlite3; import yuki_agent_connector.drivers.codex; "
            "import yuki_agent_connector.drivers.claude; "
            "import yuki_agent_connector.drivers.acp; import yuki_agent_connector.claude_inspect",
            cwd=relocated,
        )
        with tempfile.TemporaryDirectory(prefix="yuki-runtime-status-") as data:
            run(
                str(moved_python),
                "-I",
                "-B",
                "-m",
                "yuki_agent_connector",
                "--data-dir",
                data,
                "status",
                "--json",
                cwd=relocated,
            )
    finally:
        relocated.rename(output)
    legal = output / "legal"
    legal.mkdir()
    shutil.copy2(root.parents[1] / "LICENSE", legal / "Yuki-LICENSE")
    shutil.copy2(root / "uv.lock", legal / "dependency-lock.toml")
    metadata = tomllib.loads((root / "pyproject.toml").read_text())
    entries = []
    for path in sorted(output.rglob("*")):
        relative = path.relative_to(output).as_posix()
        if path.is_symlink():
            entries.append({"path": relative, "link": os.readlink(path)})
        elif path.is_file():
            entries.append({"path": relative, "sha256": digest(path)})
    manifest = {
        "version": metadata["project"]["version"],
        "protocol": 3,
        "platform": target_os,
        "arch": arch,
        "executable": python.relative_to(output).as_posix(),
        "arguments": ["-I", "-B", "-m", "yuki_agent_connector"],
        "python": {"version": PYTHON_VERSION, "url": url, "sha256": expected},
        "files": entries,
    }
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps({"runtime": str(output), "version": manifest["version"], "files": len(entries)})
    )


if __name__ == "__main__":
    main()
