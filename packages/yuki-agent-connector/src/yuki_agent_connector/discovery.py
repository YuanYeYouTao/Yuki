"""Installed native agent locations, not credentials or a second runtime."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

from .config import Instance


def candidates(driver: str = "codex") -> list[dict[str, Any]]:
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
                "driver": driver,
            },
        )

    # Registered app locations survive an application version directory change.
    if driver == "codex" and sys.platform == "win32":
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
    elif driver == "codex" and sys.platform == "darwin":
        for applications in (Path("/Applications"), Path.home() / "Applications"):
            app = applications / "Codex.app"
            add(app / "Contents/Resources/codex", f"app:{app}", "Codex App")
    executable = driver + (".exe" if sys.platform == "win32" else "")
    label = "Claude Code" if driver == "claude" else "Codex CLI"
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        path = Path(directory)
        if directory and path.is_absolute():
            add(path / executable, f"path:{path / executable}", label)
    if driver == "claude":
        path = Path.home() / ".local/bin" / executable
        add(path, f"path:{path}", label)
    if sys.platform != "win32":
        for path in (
            Path.home() / ".local/bin" / driver,
            Path("/opt/homebrew/bin") / driver,
            Path("/usr/local/bin") / driver,
        ):
            add(path, f"path:{path}", label)
    return list(found.values())


def refresh(spec: Instance) -> Instance:
    if not spec.installation:
        return spec
    for candidate in candidates() if spec.driver == "codex" else candidates(spec.driver):
        if candidate["source"] == spec.installation:
            return replace(spec, command=candidate["command"])
    raise FileNotFoundError("The selected native installation is no longer available")


async def diagnose(spec: Instance) -> dict[str, object]:
    from .drivers import DriverError
    from .drivers.codex import CodexRPC

    async def discard(message: Any) -> None:
        pass

    if spec.driver == "claude":
        try:
            spec = refresh(spec)
            environment = spec.environment()
            if (
                environment.get("CLAUDE_CONFIG_DIR")
                and not Path(environment["CLAUDE_CONFIG_DIR"]).is_absolute()
            ):
                environment["CLAUDE_CONFIG_DIR"] = str(
                    (Path(spec.cwd) / environment["CLAUDE_CONFIG_DIR"]).resolve()
                )
            process = await asyncio.create_subprocess_exec(
                spec.command,
                *spec.args,
                "auth",
                "status",
                env=environment,
                cwd=str(Path.home()),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            )
            # The native command reports login state without creating a session.
            # Keep its account information and all native output out of IPC/logs.
            output, _ = await process.communicate()
            account = json.loads(output)
            if process.returncode not in (0, 1) or not isinstance(account.get("loggedIn"), bool):
                return {"id": spec.id, "available": False, "problem": "native_diagnostic_failed"}
            available = process.returncode == 0 and account["loggedIn"]
            return {
                "id": spec.id,
                "available": available,
                **({"problem": "native_login_required"} if not available else {}),
            }
        except FileNotFoundError:
            return {"id": spec.id, "available": False, "problem": "native_not_installed"}
        except (OSError, ValueError, AttributeError):
            return {"id": spec.id, "available": False, "problem": "native_diagnostic_failed"}
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
