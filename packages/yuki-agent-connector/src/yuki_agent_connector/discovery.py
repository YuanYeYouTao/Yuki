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

if sys.platform != "win32":
    creation_flags = 0
else:
    creation_flags = subprocess.CREATE_NO_WINDOW


NPM_WINDOWS_TARGETS = {
    "x86_64-pc-windows-msvc": "@openai/codex-win32-x64",
    "aarch64-pc-windows-msvc": "@openai/codex-win32-arm64",
}


def installed_executable(path: Path) -> bool:
    return (
        path.is_absolute()
        and path.is_file()
        and (path.suffix.lower() == ".exe" if sys.platform == "win32" else os.access(path, os.X_OK))
    )


def npm_codex(package: Path, target: str) -> Path | None:
    """Resolve the active npm package, never sibling update leftovers."""
    if not (package / "package.json").is_file():
        return None
    dependency = NPM_WINDOWS_TARGETS[target]
    # npm can nest the optional native package or hoist it beside @openai/codex.
    for modules in (package / "node_modules", package.parent.parent):
        native = modules / dependency
        if (native / "package.json").is_file():
            vendor = native / "vendor"
            break
    else:
        # The original npm distribution shipped vendor within the main package.
        vendor = package / "vendor"
    for relative in ("bin/codex.exe", "codex/codex.exe"):
        executable = vendor / target / relative
        if executable.is_file():
            return executable
    return None


def codex_package(manifest: Path) -> Path | None:
    """The selected native package owns its entrypoint and current-version link."""
    if not manifest.is_file():
        return None
    metadata = json.loads(manifest.read_text(encoding="utf-8"))
    entrypoint: str = metadata["entrypoint"]
    return manifest.parent / entrypoint


def npm_claude(package: Path) -> Path | None:
    """The current npm package declares its executable, including native bins."""
    manifest = package / "package.json"
    if not manifest.is_file():
        return None
    entrypoint = json.loads(manifest.read_text(encoding="utf-8")).get("bin")
    if isinstance(entrypoint, dict):
        entrypoint = entrypoint.get("claude")
    return package / entrypoint if isinstance(entrypoint, str) else None


def candidates(driver: str = "codex") -> list[dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}

    def add(path: Path, source: str, label: str) -> None:
        if not installed_executable(path):
            return
        key = hashlib.sha256(source.encode()).hexdigest()
        found.setdefault(
            str(path.resolve()),
            {
                "id": key,
                "label": label,
                "command": str(path),
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
    if driver == "codex":
        home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser().absolute()
        manifest = home / "packages/app-server-daemon/current/codex-package.json"
        command = codex_package(manifest)
        if command is not None:
            add(command, f"codex-package:{manifest}", "Codex daemon")
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        path = Path(directory)
        if directory and path.is_absolute():
            add(path / executable, f"path:{path / executable}", label)
            if driver == "claude":
                packages = [path / "node_modules/@anthropic-ai/claude-code"]
                if sys.platform != "win32":
                    packages.append(path.parent / "lib/node_modules/@anthropic-ai/claude-code")
                for package in packages:
                    command = npm_claude(package)
                    if command is not None:
                        add(command, f"claude-npm:{package}", "Claude Code (npm)")
            if driver == "codex" and sys.platform == "win32":
                package = path / "node_modules/@openai/codex"
                for target in NPM_WINDOWS_TARGETS:
                    command = npm_codex(package, target)
                    if command is not None:
                        add(command, f"npm:{target}:{package}", "Codex CLI (npm)")
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
    command = None
    if spec.driver == "codex" and spec.installation.startswith("codex-package:"):
        command = codex_package(Path(spec.installation.removeprefix("codex-package:")))
    elif spec.driver == "codex" and spec.installation.startswith("npm:"):
        _, target, package = spec.installation.split(":", 2)
        command = npm_codex(Path(package), target)
    elif spec.driver == "claude" and spec.installation.startswith("claude-npm:"):
        command = npm_claude(Path(spec.installation.removeprefix("claude-npm:")))
    else:
        for candidate in candidates() if spec.driver == "codex" else candidates(spec.driver):
            if candidate["source"] == spec.installation:
                return replace(spec, command=candidate["command"])
    if command is not None and installed_executable(command):
        return replace(spec, command=str(command))
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
                creationflags=creation_flags,
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
