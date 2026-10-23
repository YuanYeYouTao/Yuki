"""User-level OS service integration, independent of the Desktop window."""

from __future__ import annotations

import json
import os
import plistlib
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

from .config import private_path

if sys.platform != "win32":
    creation_flags = 0

    def user_id() -> int:
        return os.getuid()
else:
    creation_flags = subprocess.CREATE_NO_WINDOW

    def user_id() -> int:
        raise OSError("POSIX user service requested on Windows")


def windows_process_handle(path: Path) -> object | None:
    """Wait handle for the process owning this installation's existing lock."""
    if sys.platform != "win32" or not path.exists():
        return None
    import ctypes
    import msvcrt
    from ctypes import wintypes

    with path.open("r+b") as stream:
        try:
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            stream.seek(1)
            pid = stream.read().decode("ascii")
        else:
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            return None
    if not pid:
        return None
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    handle = kernel.OpenProcess(0x00100000, False, int(pid))
    return handle or None


def wait_windows_process(handle: object | None) -> None:
    if sys.platform != "win32" or handle is None:
        return
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    try:
        if kernel.WaitForSingleObject(handle, 0xFFFFFFFF) == 0xFFFFFFFF:
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        kernel.CloseHandle(handle)


def run(command: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        check=check,
        capture_output=True,
        text=True,
        encoding="utf-8" if command[0].lower() == "powershell" else None,
        creationflags=creation_flags,
    )


def quote_systemd(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'


@dataclass
class UserService:
    config: Path
    data: Path
    platform: str = sys.platform

    @property
    def name(self) -> str:
        # The stable installation directory differentiates independently paired
        # devices. Service name is local packaging identity, never Person identity.
        import hashlib

        suffix = hashlib.sha256(str(self.data.resolve()).encode()).hexdigest()[:12]
        return f"yuki-agent-connector-{suffix}"

    @property
    def arguments(self) -> list[str]:
        return [
            *(["-I", "-B"] if sys.flags.isolated else []),
            "-m",
            "yuki_agent_connector",
            "--config",
            str(self.config.resolve()),
            "--data-dir",
            str(self.data.resolve()),
            "run",
        ]

    @property
    def artifact(self) -> Path:
        if self.platform == "win32":
            return self.data / "service.xml"
        if self.platform == "darwin":
            return Path.home() / "Library" / "LaunchAgents" / f"{self.name}.plist"
        return Path.home() / ".config" / "systemd" / "user" / f"{self.name}.service"

    def install(self) -> None:
        self.data.mkdir(parents=True, exist_ok=True)
        private_path(self.data, directory=True)
        executable = sys.executable
        if self.platform == "win32":
            import csv
            import io

            sid = next(
                csv.reader(io.StringIO(run(["whoami", "/user", "/fo", "csv", "/nh"]).stdout))
            )[1]
            # Windows' per-user scheduled task uses the logged-in user's token,
            # never SYSTEM, a stored password or an elevated account.
            ns = "http://schemas.microsoft.com/windows/2004/02/mit/task"
            ET.register_namespace("", ns)
            task = ET.Element(f"{{{ns}}}Task", {"version": "1.2"})
            triggers = ET.SubElement(task, "Triggers")
            trigger = ET.SubElement(triggers, "LogonTrigger")
            ET.SubElement(trigger, "UserId").text = sid
            principals = ET.SubElement(task, "Principals")
            principal = ET.SubElement(principals, "Principal", {"id": "Author"})
            for key, value in {
                "UserId": sid,
                "LogonType": "InteractiveToken",
                "RunLevel": "LeastPrivilege",
            }.items():
                ET.SubElement(principal, key).text = value
            settings = ET.SubElement(task, "Settings")
            for key, value in {
                "MultipleInstancesPolicy": "IgnoreNew",
                "ExecutionTimeLimit": "PT0S",
                "DisallowStartIfOnBatteries": "false",
                "StopIfGoingOnBatteries": "false",
            }.items():
                ET.SubElement(settings, key).text = value
            restart = ET.SubElement(settings, "RestartOnFailure")
            ET.SubElement(restart, "Interval").text = "PT1M"
            ET.SubElement(restart, "Count").text = "999"
            actions = ET.SubElement(task, "Actions", {"Context": "Author"})
            action = ET.SubElement(actions, "Exec")
            background = Path(executable).with_name("pythonw.exe")
            ET.SubElement(action, "Command").text = str(
                background if background.exists() else executable
            )
            ET.SubElement(action, "Arguments").text = subprocess.list2cmdline(self.arguments)
            self.artifact.write_bytes(ET.tostring(task, encoding="utf-16", xml_declaration=True))
            private_path(self.artifact)
            run(["schtasks", "/Create", "/TN", self.name, "/XML", str(self.artifact), "/F"])
        elif self.platform == "darwin":
            self.artifact.parent.mkdir(parents=True, exist_ok=True)
            self.artifact.write_bytes(
                plistlib.dumps(
                    {
                        "Label": self.name,
                        "ProgramArguments": [executable, *self.arguments],
                        "RunAtLoad": True,
                        "KeepAlive": {"SuccessfulExit": False},
                        "StandardOutPath": str(self.data / "service.log"),
                        "StandardErrorPath": str(self.data / "service.log"),
                        "EnvironmentVariables": self.environment(),
                    }
                )
            )
            private_path(self.artifact)
        else:
            self.artifact.parent.mkdir(parents=True, exist_ok=True)
            environment = "\n".join(
                "Environment=" + quote_systemd(f"{key}={value}")
                for key, value in self.environment().items()
            )
            command = " ".join(quote_systemd(arg) for arg in [executable, *self.arguments])
            self.artifact.write_text(
                f"[Unit]\nDescription=Yuki local agent connector\n"
                f"[Service]\nType=simple\nExecStart={command}\n{environment}\n"
                "Restart=on-failure\n[Install]\nWantedBy=default.target\n",
                encoding="utf-8",
            )
            private_path(self.artifact)
            run(["systemctl", "--user", "daemon-reload"])
            run(["systemctl", "--user", "enable", self.name])

    @staticmethod
    def environment() -> dict[str, str]:
        # Native credentials remain in their own stores. Preserve installation
        # locations and standard proxy/CA selection when a user service starts.
        names = (
            "HOME",
            "USERPROFILE",
            "PATH",
            "CODEX_HOME",
            "CLAUDE_CONFIG_DIR",
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "NO_PROXY",
            "http_proxy",
            "https_proxy",
            "all_proxy",
            "no_proxy",
            "SSL_CERT_FILE",
            "SSL_CERT_DIR",
            "NODE_EXTRA_CA_CERTS",
        )
        return {name: os.environ[name] for name in names if name in os.environ}

    def start(self) -> str:
        if self.platform == "win32":
            return run(["schtasks", "/Run", "/TN", self.name]).stdout
        if self.platform == "darwin":
            return run(["launchctl", "bootstrap", f"gui/{user_id()}", str(self.artifact)]).stdout
        return run(["systemctl", "--user", "start", self.name]).stdout

    def stop(self) -> str:
        if self.platform == "win32":
            handle = windows_process_handle(self.data / "connector.lock")
            result = run(["schtasks", "/End", "/TN", self.name], check=False)
            wait_windows_process(handle)
            return result.stdout
        if self.platform == "darwin":
            return run(["launchctl", "bootout", f"gui/{user_id()}/{self.name}"], check=False).stdout
        return run(["systemctl", "--user", "stop", self.name], check=False).stdout

    def status(self) -> str:
        if self.platform == "win32":
            return run(["schtasks", "/Query", "/TN", self.name, "/FO", "LIST"], check=False).stdout
        if self.platform == "darwin":
            return run(["launchctl", "print", f"gui/{user_id()}/{self.name}"], check=False).stdout
        return run(["systemctl", "--user", "status", self.name, "--no-pager"], check=False).stdout

    def state(self) -> dict[str, object]:
        installed = self.artifact.exists()
        if not installed:
            return {"installed": False, "running": False}
        if self.platform == "win32":
            result = run(
                [
                    "powershell",
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    "[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false); "
                    f"$t=Get-ScheduledTask -TaskName '{self.name}' -ErrorAction Stop; "
                    "[PSCustomObject]@{running=($t.State -eq 'Running');"
                    "executable=$t.Actions[0].Execute;argumentLine=$t.Actions[0].Arguments}"
                    " | ConvertTo-Json -Compress",
                ],
                check=False,
            )
            if result.returncode == 0:
                actual = json.loads(result.stdout)
                return {"installed": True, **actual}
            running = None
        elif self.platform == "darwin":
            result = run(["launchctl", "print", f"gui/{user_id()}/{self.name}"], check=False)
            running = "state = running" in result.stdout if result.returncode == 0 else False
            actual = re.search(r"^\s*program = (.+)$", result.stdout, re.MULTILINE)
            if actual:
                return {"installed": True, "running": running, "executable": actual[1]}
            # An unloaded LaunchAgent still has its exact next-launch target.
            value = plistlib.loads(self.artifact.read_bytes())
            return {
                "installed": True,
                "running": running,
                "executable": value["ProgramArguments"][0],
                "arguments": value["ProgramArguments"][1:],
            }
        else:
            result = run(
                ["systemctl", "--user", "show", self.name, "-p", "ActiveState", "-p", "ExecStart"],
                check=False,
            )
            running = "ActiveState=active" in result.stdout if result.returncode == 0 else None
            actual = re.search(r"path=(.+?) ;", result.stdout)
            if actual:
                return {"installed": True, "running": running, "executable": actual[1]}
        return {"installed": True, "running": running}

    def uninstall(self) -> None:
        self.stop()
        if self.platform == "win32":
            run(["schtasks", "/Delete", "/TN", self.name, "/F"])
        elif self.platform != "darwin":
            run(["systemctl", "--user", "disable", self.name])
        self.artifact.unlink(missing_ok=True)
        if self.platform not in {"darwin", "win32"}:
            run(["systemctl", "--user", "daemon-reload"])
        # Journal/config and all native sessions belong to the user and survive.

    def describe(self) -> str:
        return json.dumps(
            {"name": self.name, "platform": self.platform, "artifact": str(self.artifact)},
            ensure_ascii=False,
        )
