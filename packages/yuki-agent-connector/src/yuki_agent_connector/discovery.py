"""Installed Codex locations, not credentials or a second native runtime."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

from .config import Instance


def candidates() -> list[dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}

    def add(path: Path, source: str, label: str) -> None:
        if not path.is_absolute() or not path.is_file():
            return
        if sys.platform == "win32" and path.suffix.lower() != ".exe":
            return
        if sys.platform != "win32" and not os.access(path, os.X_OK):
            return
        key = hashlib.sha256(source.encode()).hexdigest()
        found.setdefault(
            str(path.resolve()),
            {
                "id": key,
                "label": label,
                "command": str(path.resolve()),
                "source": source,
            },
        )

    # Registered app locations survive an application version directory change.
    if sys.platform == "win32":
        result = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false); "
                "Get-AppxPackage -Name '*Codex*' | Select-Object Name,InstallLocation | "
                "ConvertTo-Json -Compress",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            creationflags=subprocess.CREATE_NO_WINDOW,
            check=False,
        )
        packages = (
            json.loads(result.stdout) if result.returncode == 0 and result.stdout.strip() else []
        )
        if isinstance(packages, dict):
            packages = [packages]
        for package in packages:
            location = Path(package["InstallLocation"])
            for relative in ("app/resources/codex.exe", "resources/codex.exe"):
                add(location / relative, f"appx:{package['Name']}:{relative}", package["Name"])
    elif sys.platform == "darwin":
        for directory in (Path("/Applications"), Path.home() / "Applications"):
            app = directory / "Codex.app"
            add(app / "Contents/Resources/codex", f"app:{app}", "Codex App")
    executable = "codex.exe" if sys.platform == "win32" else "codex"
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        path = Path(directory)
        if directory and path.is_absolute():
            add(path / executable, f"path:{path / executable}", "Codex CLI")
    if sys.platform != "win32":
        for path in (
            Path.home() / ".local/bin/codex",
            Path("/opt/homebrew/bin/codex"),
            Path("/usr/local/bin/codex"),
        ):
            add(path, f"path:{path}", "Codex CLI")
    return list(found.values())


def refresh(spec: Instance) -> Instance:
    if not spec.installation:
        return spec
    for candidate in candidates():
        if candidate["source"] == spec.installation:
            return replace(spec, command=candidate["command"])
    raise FileNotFoundError("The selected Codex installation is no longer available")


async def diagnose(spec: Instance) -> dict[str, object]:
    from .drivers import DriverError
    from .drivers.codex import CodexRPC

    async def discard(message: Any) -> None:
        pass

    if spec.driver != "codex":
        return {"id": spec.id, "available": False, "problem": "diagnostic_not_supported"}
    rpc = CodexRPC(spec, discard)
    try:
        await rpc.start()
        account = await rpc.call("account/read", {"refreshToken": False})
        needs_login = account.get("requiresOpenaiAuth", False) and not account.get("account")
        return {
            "id": spec.id,
            "available": not needs_login,
            **({"problem": "native_login_required"} if needs_login else {}),
        }
    except FileNotFoundError:
        return {"id": spec.id, "available": False, "problem": "native_not_installed"}
    except (ConnectionError, OSError):
        return {"id": spec.id, "available": False, "problem": "native_owner_unavailable"}
    except DriverError as error:
        return {"id": spec.id, "available": False, "problem": error.code}
    finally:
        await rpc.stop()
