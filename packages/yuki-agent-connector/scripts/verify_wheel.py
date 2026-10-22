"""Install the built artifact in a clean environment without the Bot project."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path


def run(*arguments: str, cwd: Path) -> None:
    subprocess.run(
        arguments,
        cwd=cwd,
        check=True,
        env={
            key: value
            for key, value in os.environ.items()
            if key not in {"PYTHONPATH", "VIRTUAL_ENV"}
        },
    )


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    metadata = tomllib.loads((root / "pyproject.toml").read_text())
    wheel = (
        Path(sys.argv[1]).resolve()
        if len(sys.argv) > 1
        else (
            root
            / "dist"
            / f"yuki_agent_connector-{metadata['project']['version']}-py3-none-any.whl"
        )
    )
    with tempfile.TemporaryDirectory(prefix="yuki-connector-wheel-") as temporary:
        target = Path(temporary)
        run("uv", "venv", "--python", "3.12", str(target / "env"), cwd=target)
        python = target / "env" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        requirements = target / "requirements.txt"
        run(
            "uv",
            "export",
            "--quiet",
            "--frozen",
            "--no-dev",
            "--no-emit-project",
            "--output-file",
            str(requirements),
            cwd=root,
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
            cwd=target,
        )
        run("uv", "pip", "install", "--python", str(python), "--no-deps", str(wheel), cwd=target)
        run(str(python), "-m", "yuki_agent_connector", "--version", cwd=target)
        run(
            str(python),
            "-m",
            "yuki_agent_connector",
            "--data-dir",
            str(target / "data"),
            "status",
            "--json",
            cwd=target,
        )
        run(
            str(python),
            "-c",
            "import importlib.util; "
            "assert importlib.util.find_spec('yuki') is None; "
            "import yuki_agent_connector.drivers.codex; "
            "import yuki_agent_connector.drivers.claude; "
            "import yuki_agent_connector.drivers.acp",
            cwd=target,
        )


if __name__ == "__main__":
    main()
