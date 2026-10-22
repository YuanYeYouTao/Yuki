from __future__ import annotations

import io
import json
import plistlib
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import httpx
import pytest

from yuki_agent_connector import cli, service
from yuki_agent_connector.config import Config, Instance


def test_pair_exchanges_explicit_code_and_status_never_exposes_secrets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    requests: list[dict[str, Any]] = []

    def post(url: str, *, json: Any) -> httpx.Response:
        requests.append({"url": url, "json": json})
        return httpx.Response(
            200,
            json={"device_id": "internal-device", "token": "PRIVATE_TOKEN"},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(cli.httpx, "post", post)
    monkeypatch.setattr(sys, "stdin", io.StringIO("ONE_TIME_CODE\n"))
    args = ["--data-dir", str(tmp_path / "device")]
    assert cli.main([*args, "status", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "paired": False,
        "instances": [],
        "service": {"installed": False, "running": False},
    }
    assert (
        cli.main(
            [*args, "pair", "--url", "https://yuki.example", "--label", "Laptop", "--code-stdin"]
        )
        == 0
    )
    assert requests == [
        {
            "url": "https://yuki.example/agents/v1/pair",
            "json": {
                "code": "ONE_TIME_CODE",
                "device_label": "Laptop",
            },
        }
    ]
    output = capsys.readouterr().out
    assert "PRIVATE_TOKEN" not in output and "ONE_TIME_CODE" not in output
    saved = Config.load(tmp_path / "device" / "config.json")
    assert saved.url == "wss://yuki.example/agents/v1/connect"
    assert saved.token == "PRIVATE_TOKEN"
    assert (
        cli.main(
            [
                *args,
                "instance",
                "add",
                "--label",
                "Codex",
                "--driver",
                "codex",
                "--command",
                sys.executable,
                "--cwd",
                str(tmp_path),
                "--env",
                "SECRET=must-not-leak",
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert cli.main([*args, "status", "--json"]) == 0
    status = capsys.readouterr().out
    assert json.loads(status)["device_id"] == "internal-device"
    assert (
        "SECRET" not in status and "must-not-leak" not in status and "PRIVATE_TOKEN" not in status
    )


@pytest.mark.parametrize("platform", ["win32", "darwin", "linux"])
def test_user_service_install_lifecycle_keeps_native_and_journal(
    platform: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    data = tmp_path / "local-service"
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setattr(service, "user_id", lambda: 1000)
    calls: list[list[str]] = []

    def run(command: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        output = '"user","S-1-5-21-1"' if command[0] == "whoami" else ""
        if command[0] == "powershell":
            output = "4\n"
        elif "ActiveState" in command:
            output = "active\n"
        elif command[:2] == ["launchctl", "print"]:
            output = "state = running"
        return subprocess.CompletedProcess(command, 0, output, "")

    monkeypatch.setattr(service, "run", run)
    configured = service.UserService(data / "config.json", data, platform)
    data.mkdir()
    (data / "journal.sqlite3").write_text("original local receipts")
    Config(
        "wss://example/agents/v1/connect",
        "d",
        "t",
        [
            Instance("original", "codex", "Native", "native-codex"),
        ],
    ).save(data / "config.json")
    configured.install()
    if platform == "win32":
        root = ET.fromstring(configured.artifact.read_bytes())
        namespace = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
        assert (
            root.findtext("t:Principals/t:Principal/t:LogonType", namespaces=namespace)
            == "InteractiveToken"
        )
        assert (
            root.findtext("t:Principals/t:Principal/t:RunLevel", namespaces=namespace)
            == "LeastPrivilege"
        )
    elif platform == "darwin":
        manifest = plistlib.loads(configured.artifact.read_bytes())
        assert manifest["ProgramArguments"][0] == sys.executable
        assert "yuki_agent_connector" in manifest["ProgramArguments"]
    else:
        unit = configured.artifact.read_text()
        assert "User=root" not in unit and "ExecStart=" in unit
        assert all("--user" in call for call in calls if call[0] == "systemctl")
    configured.start()
    assert configured.state() == {"installed": True, "running": True}
    configured.stop()
    configured.uninstall()
    assert not configured.artifact.exists()
    assert (data / "journal.sqlite3").read_text() == "original local receipts"
    assert Config.load(data / "config.json").instances[0].id == "original"
    assert all(call[0] not in {"taskkill", "kill", "codex", "claude"} for call in calls)
