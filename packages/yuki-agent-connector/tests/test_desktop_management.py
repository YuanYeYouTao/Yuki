import io
import json
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from yuki_agent_connector import cli, discovery
from yuki_agent_connector.config import Config, Instance


def test_configure_preserves_original_identity_environment_and_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "config.json"
    original = Instance(
        "original",
        "codex",
        "Old",
        sys.executable,
        cwd=str(tmp_path),
        env={"SECRET": "private"},
        endpoint="ws://original-owner",
    )
    Config("wss://example/agents/v1/connect", "device", "private-token", [original]).save(path)
    journal = tmp_path / "journal.sqlite3"
    journal.write_bytes(b"original receipts")
    monkeypatch.setattr(
        cli,
        "candidates",
        lambda: [{"id": "candidate", "source": "appx:stable", "command": sys.executable}],
    )
    monkeypatch.setattr(
        sys,
        "stdin",
        io.TextIOWrapper(
            io.BytesIO(
                json.dumps(
                    {
                        "id": "original",
                        "candidateId": "candidate",
                        "label": "New",
                        "cwd": str(tmp_path),
                    }
                ).encode("utf-8")
            ),
            encoding="utf-8",
        ),
    )
    assert cli.main(["--data-dir", str(tmp_path), "instance", "configure"]) == 0
    current = Config.load(path)
    assert current.instances == [replace(original, label="New", installation="appx:stable")]
    assert current.device_id == "device" and current.token == "private-token"
    assert journal.read_bytes() == b"original receipts"
    assert "private" not in capsys.readouterr().out


def test_refresh_tracks_registered_installation_without_changing_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = Instance(
        "original",
        "codex",
        "Codex",
        "old/executable",
        installation="appx:stable",
        env={"HOME": "original"},
        cwd="original-project",
    )
    monkeypatch.setattr(
        discovery,
        "candidates",
        lambda: [
            {"source": "appx:other", "command": "other"},
            {"source": "appx:stable", "command": "new/executable"},
        ],
    )
    assert discovery.refresh(original) == replace(original, command="new/executable")
    monkeypatch.setattr(discovery, "candidates", lambda: [])
    with pytest.raises(FileNotFoundError):
        discovery.refresh(original)


@pytest.mark.parametrize("registered", [True, False])
def test_discover_instance_resolves_only_its_original_installation_without_writing(
    registered: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    original = Instance(
        "original",
        "codex",
        "Codex",
        "old/executable",
        installation="appx:stable" if registered else None,
        env={"SECRET": "private"},
        cwd="original-project",
    )
    path = tmp_path / "config.json"
    Config("wss://example/agents/v1/connect", "device", "private-token", [original]).save(path)
    journal = tmp_path / "journal.sqlite3"
    journal.write_bytes(b"original receipts")
    before = path.read_bytes()
    monkeypatch.setattr(
        discovery,
        "candidates",
        lambda: [
            {"id": "other", "source": "appx:other", "command": "other/executable"},
            {"id": "stable", "source": "appx:stable", "command": "new/executable"},
        ],
    )
    assert (
        cli.main(["--data-dir", str(tmp_path), "discover", "--instance", "original", "--json"]) == 0
    )
    assert json.loads(capsys.readouterr().out) == {
        "instance_id": "original",
        "command": "new/executable" if registered else original.command,
    }
    assert path.read_bytes() == before
    assert journal.read_bytes() == b"original receipts"


@pytest.mark.parametrize(
    ("identity", "problem"),
    [("missing", "connector_configuration_failed"), ("original", "native_not_installed")],
)
def test_discover_instance_keeps_missing_identity_and_installation_as_real_errors(
    identity: str,
    problem: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    original = Instance("original", "codex", "Codex", "old/executable", installation="appx:stable")
    path = tmp_path / "config.json"
    Config("wss://example/agents/v1/connect", "device", "private-token", [original]).save(path)
    before = path.read_bytes()
    monkeypatch.setattr(
        discovery,
        "candidates",
        lambda: [{"id": "other", "source": "appx:other", "command": "other"}],
    )
    assert (
        cli.main(["--data-dir", str(tmp_path), "discover", "--instance", identity, "--json"]) == 1
    )
    assert json.loads(capsys.readouterr().out) == {"problem": problem}
    assert path.read_bytes() == before


@pytest.mark.parametrize("manual", [False, True])
def test_editing_after_upgrade_preserves_original_profile_or_explicit_manual_choice(
    manual: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    old = tmp_path / "old-version" / "codex.exe"
    current = tmp_path / "new-version" / "codex.exe"
    current.parent.mkdir()
    current.write_bytes(b"installed executable, never launched")
    project = tmp_path / "new-project"
    project.mkdir()
    original = Instance(
        "original",
        "codex",
        "Old label",
        str(old),
        args=["--original-option"],
        cwd=str(tmp_path),
        env={"SECRET": "private"},
        socket="original-owner",
        installation="appx:stable",
    )
    path = tmp_path / "config.json"
    config = Config("wss://example/agents/v1/connect", "device", "private-token", [original])
    config.save(path)
    journal = tmp_path / "journal.sqlite3"
    journal.write_bytes(b"original receipts")

    def candidates() -> list[dict[str, str]]:
        return [{"id": "stable", "source": "appx:stable", "command": str(current)}]

    monkeypatch.setattr(cli, "candidates", candidates)
    monkeypatch.setattr(discovery, "candidates", candidates)
    monkeypatch.setattr(
        sys,
        "stdin",
        io.TextIOWrapper(
            io.BytesIO(
                json.dumps(
                    {
                        "id": original.id,
                        "label": "New label",
                        "command": str(current if manual else old),
                        "cwd": str(project),
                    }
                ).encode("utf-8")
            ),
            encoding="utf-8",
        ),
    )
    assert cli.main(["--data-dir", str(tmp_path), "instance", "configure"]) == 0
    assert json.loads(capsys.readouterr().out) == {"instance_id": original.id}
    assert Config.load(path) == replace(
        config,
        instances=[
            replace(
                original,
                label="New label",
                command=str(current),
                cwd=str(project),
                installation=None if manual else original.installation,
            )
        ],
    )
    assert journal.read_bytes() == b"original receipts"


@pytest.mark.parametrize("existing", [True, False])
def test_configure_without_project_uses_original_instance_cwd(
    existing: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "config.json"
    original = Instance("original", "codex", "Old", sys.executable, cwd=tmp_path.as_posix())
    Config(
        "wss://example/agents/v1/connect",
        "device",
        "private-token",
        [original] if existing else [],
    ).save(path)
    monkeypatch.setattr(cli, "candidates", lambda: [])
    value = {"command": sys.executable, "label": "Codex"}
    if existing:
        value["id"] = original.id
    monkeypatch.setattr(
        sys,
        "stdin",
        io.TextIOWrapper(io.BytesIO(json.dumps(value).encode("utf-8")), encoding="utf-8"),
    )

    assert cli.main(["--data-dir", str(tmp_path), "instance", "configure"]) == 0

    current = Config.load(path)
    assert len(current.instances) == 1
    spec = current.instances[0]
    assert spec.cwd == (original.cwd if existing else str(Path.home()))
    assert spec.command == original.command
    if existing:
        assert spec.id == original.id
    assert current.device_id == "device" and current.token == "private-token"
    receipt = json.loads(capsys.readouterr().out)
    assert receipt == {"instance_id": spec.id}

    # A later service start can fail independently. Continue with the returned
    # saved identity rather than registering another instance on retry.
    value["id"] = receipt["instance_id"]
    monkeypatch.setattr(
        sys,
        "stdin",
        io.TextIOWrapper(io.BytesIO(json.dumps(value).encode("utf-8")), encoding="utf-8"),
    )
    assert cli.main(["--data-dir", str(tmp_path), "instance", "configure"]) == 0
    assert json.loads(capsys.readouterr().out) == receipt
    assert Config.load(path) == current


@pytest.mark.parametrize("logged_in", [True, False])
async def test_diagnose_reads_account_without_model_or_auth_files(
    logged_in: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    from yuki_agent_connector.drivers import codex

    calls: list[Any] = []

    class RPC:
        def __init__(self, spec: Instance, emit: Any):
            pass

        async def start(self) -> None:
            calls.append("start")

        async def call(self, method: str, args: Any) -> Any:
            calls.append((method, args))
            return {"requiresOpenaiAuth": True, "account": {"id": "private"} if logged_in else None}

        async def stop(self) -> None:
            calls.append("stop")

    monkeypatch.setattr(codex, "CodexRPC", RPC)
    result = await discovery.diagnose(Instance("original", "codex", "Codex", "not-executed"))
    assert result["available"] is logged_in
    assert calls == ["start", ("account/read", {"refreshToken": False}), "stop"]
    assert "private" not in json.dumps(result)


def test_discovery_never_searches_cwd(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "codex").write_text("not executed")
    monkeypatch.setenv("PATH", ".;")
    monkeypatch.setattr(discovery.sys, "platform", "linux")
    monkeypatch.setattr(discovery.os, "access", lambda *_: False)
    assert discovery.candidates() == []


@pytest.mark.skipif(sys.platform != "win32", reason="Real Windows PowerShell UTF-8 pipe")
def test_windows_service_json_uses_explicit_utf8_for_unicode_paths() -> None:
    from yuki_agent_connector.service import run

    result = run(
        [
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false); "
            "@{executable='C:/資料/連接器/python.exe'} | ConvertTo-Json -Compress",
        ]
    )
    assert json.loads(result.stdout)["executable"] == "C:/資料/連接器/python.exe"


def test_label_or_project_change_keeps_registered_installation_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = Instance(
        "original", "codex", "Old", sys.executable, cwd=str(tmp_path), installation="appx:stable"
    )
    path = tmp_path / "config.json"
    Config("wss://example/agents/v1/connect", "device", "token", [original]).save(path)
    monkeypatch.setattr(cli, "candidates", lambda: [])
    monkeypatch.setattr(
        discovery,
        "candidates",
        lambda: [{"id": "stable", "source": original.installation, "command": original.command}],
    )
    monkeypatch.setattr(
        sys,
        "stdin",
        io.TextIOWrapper(
            io.BytesIO(
                json.dumps(
                    {
                        "id": "original",
                        "command": sys.executable,
                        "label": "New",
                        "cwd": str(tmp_path),
                    }
                ).encode("utf-8")
            ),
            encoding="utf-8",
        ),
    )
    assert cli.main(["--data-dir", str(tmp_path), "instance", "configure"]) == 0
    assert Config.load(path).instances[0] == replace(original, label="New")


@pytest.mark.parametrize("errors", ["strict", "surrogateescape"])
def test_configure_reads_raw_utf8_json_bytes_under_gbk_text_stdin(
    errors: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    path = tmp_path / "config.json"
    original = Instance(
        "original-native",
        "codex",
        "Original",
        sys.executable,
        cwd=str(tmp_path),
        env={"CODEX_HOME": "original-profile"},
        endpoint="ws://original-owner",
        installation="appx:original",
    )
    before = Config(
        "wss://example.invalid/roles/original/agents/v1/connect",
        "original-device",
        "fixture-private-token",
        [original],
    )
    before.save(path)
    journal = tmp_path / "journal.sqlite3"
    journal.write_bytes(b"original operation receipts")
    value = {
        "id": original.id,
        "command": sys.executable,
        "label": "本機 Codex 🦊",
        "cwd": str(project),
    }
    raw = json.dumps(value, ensure_ascii=False).encode("utf-8")
    stdin = io.TextIOWrapper(io.BytesIO(raw), encoding="gbk", errors=errors)
    monkeypatch.setattr(sys, "stdin", stdin)
    monkeypatch.setattr(cli, "candidates", lambda: [])
    monkeypatch.setattr(
        discovery,
        "candidates",
        lambda: [{"id": "original", "source": original.installation, "command": original.command}],
    )

    assert cli.main(["--data-dir", str(tmp_path), "instance", "configure"]) == 0

    assert Config.load(path) == replace(
        before, instances=[replace(original, label=value["label"], cwd=str(project))]
    )
    assert journal.read_bytes() == b"original operation receipts"
    assert json.loads(capsys.readouterr().out) == {"instance_id": original.id}
