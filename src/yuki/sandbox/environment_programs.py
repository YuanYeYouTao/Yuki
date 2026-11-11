"""Install a browser mode's Debian packages in this environment and record what is installed.

Runs inside the environment, both when the desktop image is built and when the Manager
moves an existing rootFS to a larger profile. Standard library only. Usage:

    python environment_programs.py MODE [DEBIAN_PACKAGE...]
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

HEADLESS = ("chromium", "fonts-noto-cjk", "fonts-noto-color-emoji")
MODE_PACKAGES: dict[str, tuple[str, ...]] = {
    "none": (),
    "headless": HEADLESS,
    "headed": (
        *HEADLESS,
        "tigervnc-standalone-server",
        "openbox",
        "socat",
        "xdotool",
        "x11-utils",
        "imagemagick",
    ),
}
MANIFEST = Path("/opt/yuki-runtime/manifest.json")


def wanted(mode: str, extra: tuple[str, ...] | list[str] = ()) -> list[str]:
    return list(dict.fromkeys([*MODE_PACKAGES[mode], *extra]))


def main(argv: list[str]) -> int:
    mode, *extra = argv
    packages = wanted(mode, extra)
    if packages:
        environment = {**os.environ, "DEBIAN_FRONTEND": "noninteractive"}
        subprocess.run(["apt-get", "update", "-qq"], check=True, env=environment)
        subprocess.run(
            ["apt-get", "install", "-y", "-qq", "--no-install-recommends", "--", *packages],
            check=True,
            env=environment,
        )
        for entry in Path("/var/lib/apt/lists").iterdir():
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry)
            else:
                entry.unlink()
    # Downgrading a profile keeps software, so the record only ever grows.
    tools: dict[str, str] = {}
    if MANIFEST.exists():
        tools = {
            item["name"]: item["version"] for item in json.loads(MANIFEST.read_text())["tools"]
        }
    for package in packages:
        tools[package] = subprocess.check_output(
            ["dpkg-query", "-W", "-f=${Version}", package], text=True
        ).strip()
    MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST.write_text(
        json.dumps(
            {"tools": [{"name": name, "version": version} for name, version in tools.items()]},
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
