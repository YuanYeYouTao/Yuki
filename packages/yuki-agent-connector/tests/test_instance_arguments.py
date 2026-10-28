import io
import json
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from yuki_agent_connector import cli
from yuki_agent_connector.config import Config, Instance


@pytest.mark.parametrize("arguments", [None, [], ["--stdio", "--label", "two words", ""]])
def test_desktop_arguments_preserve_native_profile_and_original_receipts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    arguments: list[str] | None,
) -> None:
    original = Instance(
        "original-acp",
        "acp",
        "Original",
        sys.executable,
        args=["--stdio", "--profile", "original profile"],
        env={"PRIVATE_NATIVE_KEY": "native-secret"},
        socket="original-owner",
        cwd=str(tmp_path),
    )
    path = tmp_path / "config.json"
    Config("wss://example.test/agents/v1/connect", "original-device", "secret", [original]).save(
        path
    )
    journal = tmp_path / "journal.sqlite3"
    journal.write_bytes(b"original operation receipts")
    value: dict[str, object] = {
        "id": original.id,
        "label": "Renamed",
        "command": original.command,
    }
    if arguments is not None:
        value["args"] = arguments
    monkeypatch.setattr(
        sys, "stdin", io.TextIOWrapper(io.BytesIO(json.dumps(value).encode()), encoding="utf-8")
    )

    assert cli.main(["--data-dir", str(tmp_path), "instance", "configure"]) == 0
    current = Config.load(path)
    assert current.instances == [
        replace(original, label="Renamed", args=original.args if arguments is None else arguments)
    ]
    assert current.device_id == "original-device" and current.token == "secret"
    assert journal.read_bytes() == b"original operation receipts"
    capsys.readouterr()
    assert cli.main(["--data-dir", str(tmp_path), "status", "--json"]) == 0
    status = capsys.readouterr().out
    assert json.loads(status)["instances"][0]["args"] == current.instances[0].args
    assert "native-secret" not in status and "original-owner" not in status


@pytest.mark.parametrize("arguments", ["--stdio", {"command": "--stdio"}, ["--stdio", 3]])
def test_invalid_argument_shape_does_not_write_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, arguments: object
) -> None:
    path = tmp_path / "config.json"
    original = Instance("original", "acp", "Original", sys.executable)
    Config("wss://example.test/agents/v1/connect", "device", "secret", [original]).save(path)
    before = path.read_bytes()
    value = {
        "id": original.id,
        "label": original.label,
        "command": original.command,
        "args": arguments,
    }
    monkeypatch.setattr(
        sys, "stdin", io.TextIOWrapper(io.BytesIO(json.dumps(value).encode()), encoding="utf-8")
    )

    assert cli.main(["--data-dir", str(tmp_path), "instance", "configure"]) == 1
    assert path.read_bytes() == before
