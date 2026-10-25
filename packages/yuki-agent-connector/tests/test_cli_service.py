from __future__ import annotations

import io
import json
import os
import plistlib
import subprocess
import sys
import xml.etree.ElementTree as ET
from dataclasses import asdict
from pathlib import Path
from typing import Any

import httpx
import pytest

from yuki_agent_connector import __version__, cli, config, service
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
    monkeypatch.setattr(
        sys, "stdin", io.TextIOWrapper(io.BytesIO(b"ONE_TIME_CODE\n"), encoding="utf-8")
    )
    args = ["--data-dir", str(tmp_path / "device")]
    assert cli.main([*args, "status", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "version": __version__,
        "protocol": 3,
        "runtime": sys.executable,
        "data_directory": str((tmp_path / "device").resolve()),
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


@pytest.mark.parametrize("encoding", ["cp936", "cp1252"])
def test_machine_status_preserves_unicode_through_a_non_utf8_cli_pipe(
    tmp_path: Path, encoding: str
) -> None:
    directory = tmp_path / "isolated data 中文"
    directory.mkdir()
    path = directory / "config.json"
    configured = Config(
        "wss://example.invalid/agents/v1/connect",
        "isolated-device",
        "PRIVATE_TEST_TOKEN",
        [
            Instance(
                "original-profile",
                "codex",
                "家中 Codex",
                "not-executed-native-agent",
                env={"API_KEY": "PRIVATE_NATIVE_SECRET"},
            )
        ],
    )
    path.write_text(json.dumps(asdict(configured), ensure_ascii=False), encoding="utf-8")
    original = path.read_bytes()
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "yuki_agent_connector",
            "--data-dir",
            str(directory),
            "status",
            "--json",
        ],
        env={
            **os.environ,
            "PYTHONUTF8": "0",
            "PYTHONIOENCODING": encoding,
            "PYTHONPATH": os.pathsep.join(
                [str(Path(cli.__file__).resolve().parents[1]), os.environ.get("PYTHONPATH", "")]
            ),
        },
        capture_output=True,
        check=True,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )
    status = json.loads(result.stdout.decode("utf-8"))
    assert status == {
        "version": __version__,
        "protocol": 3,
        "runtime": sys.executable,
        "data_directory": str(directory.resolve()),
        "server": "https://example.invalid",
        "paired": True,
        "device_id": "isolated-device",
        "instances": [
            {
                "id": "original-profile",
                "label": "家中 Codex",
                "driver": "codex",
                "command": "not-executed-native-agent",
                "cwd": str(Path.home()),
            },
        ],
        "service": {"installed": False, "running": False},
    }
    assert "PRIVATE_TEST_TOKEN" not in result.stdout.decode("utf-8")
    assert "PRIVATE_NATIVE_SECRET" not in result.stdout.decode("utf-8")
    assert path.read_bytes() == original
    assert not (directory / "service.xml").exists()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows ACL subprocess flags")
def test_windows_private_paths_keep_acl_commands_hidden(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def run(command: list[str], **options: Any) -> subprocess.CompletedProcess[str]:
        calls.append((command, options))
        return subprocess.CompletedProcess(command, 0, '"user","S-1-5-21-1"', "")

    monkeypatch.setattr(config.subprocess, "run", run)
    config.private_path(tmp_path / "private directory 中文", directory=True)
    config.private_path(tmp_path / "private file 中文.json")
    assert [command[0] for command, _ in calls] == ["whoami", "icacls", "whoami", "icacls"]
    assert all(
        options.get("creationflags", 0) & subprocess.CREATE_NO_WINDOW for _, options in calls
    )
    assert all(options["capture_output"] and options["check"] for _, options in calls)


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
            output = json.dumps({"running": True, "executable": sys.executable})
        elif "ActiveState" in command:
            output = f"ActiveState=active\nExecStart={{ path={sys.executable} ; }}"
        elif command[:2] == ["launchctl", "print"]:
            output = f"state = running\nprogram = {sys.executable}"
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
    assert configured.state() == {"installed": True, "running": True, "executable": sys.executable}
    configured.stop()
    configured.uninstall()
    assert not configured.artifact.exists()
    assert (data / "journal.sqlite3").read_text() == "original local receipts"
    assert Config.load(data / "config.json").instances[0].id == "original"
    assert all(call[0] not in {"taskkill", "kill", "codex", "claude"} for call in calls)
