"""Readonly local capability facts; no owner or model is started here."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import Any


def computer_status() -> dict[str, Any]:
    shell = (
        shutil.which("pwsh") or shutil.which("powershell")
        if sys.platform == "win32"
        else os.environ.get("SHELL") or shutil.which("bash") or "/bin/sh"
    )
    return {
        "platform": sys.platform,
        "shell": shell,
        "home": str(Path.home()),
        "tty": False,
        "interrupt_supported": sys.platform != "win32",
    }
