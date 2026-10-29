"""Local permission, real shell ownership and original-operation receipts."""

from __future__ import annotations

import asyncio
import json
import shlex
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from yuki_agent_connector import cli
from yuki_agent_connector import config as config_module
from yuki_agent_connector import journal as journal_module
from yuki_agent_connector.config import Config, Instance
from yuki_agent_connector.connector import Connector
from yuki_agent_connector.journal import Journal
from yuki_agent_connector.terminal import LOG_SEGMENT_BYTES, LOG_SEGMENTS


@pytest.fixture
def local(tmp_path, monkeypatch):
    monkeypatch.setattr(config_module, "private_path", lambda *a, **k: None)
    monkeypatch.setattr(journal_module, "private_path", lambda *a, **k: None)
    path = tmp_path / "config.json"
    configured = Config("ws://127.0.0.1/agents/v1/connect", "device", "token")
    configured.save(path)
    journal = Journal(tmp_path / "journal.sqlite3", namespace="test-device")
    connector = Connector(configured, journal, config_path=path)
    yield connector, path
    journal.close()


def python_command(script: Path) -> str:
    if sys.platform == "win32":
        return (
            "& '" + sys.executable.replace("'", "''") + "' '" + str(script).replace("'", "''") + "'"
        )
    return shlex.join([sys.executable, str(script)])


def command(key, action, *, run="run", **arguments):
    return {
        "id": key,
        "resource": "terminal",
        "action": action,
        "run_id": run,
        "arguments": arguments,
    }


async def wait_until(predicate):
    async with asyncio.timeout(15):
        while not predicate():  # noqa: ASYNC110 - observe a real external process/file
            await asyncio.sleep(0.03)


async def test_permission_hot_toggle_preserves_original_run_and_receipts(local, tmp_path):
    connector, path = local
    script = tmp_path / "动作 中文.py"
    marker = tmp_path / "effect.txt"
    script.write_text(
        "from pathlib import Path\nprint('你好😀',flush=True)\n"
        f"Path({str(marker)!r}).write_text('once')\n",
        encoding="utf-8",
    )
    execute = command("original-exec", "exec", command=python_command(script), cwd=str(tmp_path))
    denied = await connector.dispatch(execute)
    assert denied["result"]["error"]["code"] == "full_access_disabled"
    assert not marker.exists()
    replace(Config.load(path), full_access=True).save(path)
    # The rejected original stays rejected; enabling access requires a new request.
    assert (await connector.dispatch(execute))["result"]["status"] == "failed"
    execute["id"] = "accepted-exec"
    execute["run_id"] = "accepted-run"
    result = await connector.dispatch(execute)
    assert result["result"]["admitted"]
    await asyncio.wait_for(connector.terminal.runs["accepted-run"].done.wait(), 15)
    replace(Config.load(path), full_access=False).save(path)
    read = (await connector.dispatch(command("read", "read", run="accepted-run")))["result"]
    assert (
        read["output"] == "你好😀\r\n" if sys.platform == "win32" else read["output"] == "你好😀\n"
    )
    assert read["completion"]["status"] == "succeeded"
    stable = await connector.dispatch(command("get", "get", operation_id="accepted-exec"))
    assert stable["result"]["completion"] == read["completion"]
    assert marker.read_text() == "once"
    assert not connector.drivers
    await connector.terminal.close()


def test_old_config_and_full_access_cli_keep_original_agent_grants(local, capsys):
    connector, path = local
    original = {
        "url": connector.config.url,
        "device_id": "device",
        "token": "token",
        "instances": [
            {
                "id": "original-agent",
                "driver": "codex",
                "label": "My Codex",
                "command": "original-codex",
                "env": {"CODEX_HOME": "original-profile"},
            }
        ],
    }
    path.write_text(json.dumps(original))
    assert Config.load(path).full_access is False
    for enabled in ("true", "false"):
        assert cli.main(["--config", str(path), "full-access", "--enabled", enabled, "--json"]) == 0
        assert json.loads(capsys.readouterr().out)["full_access"] is (enabled == "true")
        current = Config.load(path)
        assert current.device_id == "device" and current.token == "token"
        assert current.instances == [Instance(**original["instances"][0])]
    assert connector.journal.db.execute("SELECT COUNT(*) FROM operations").fetchone()[0] == 0


async def test_real_stdin_duplicate_exec_and_restarted_reader_do_not_restart(local, tmp_path):
    connector, path = local
    replace(Config.load(path), full_access=True).save(path)
    script = tmp_path / "stdin.py"
    marker = tmp_path / "starts"
    script.write_text(
        "import sys\nfrom pathlib import Path\n"
        f"with open({str(marker)!r},'a') as f: f.write('x')\n"
        "print('ready',flush=True)\nprint('reply:'+sys.stdin.readline(),flush=True)\n"
    )
    execute = command("stdin-exec", "exec", command=python_command(script), cwd=str(tmp_path))
    await connector.dispatch(execute)
    await wait_until(marker.exists)
    await connector.dispatch(execute)
    assert marker.read_bytes() == b"x"
    restored = Journal(connector.journal.path, namespace="test-device")
    restored_connector = Connector(Config.load(path), restored, config_path=path)
    try:
        original = (await restored_connector.dispatch(command("restored", "read")))["result"]
        assert original["status"] == "unknown" and original["reason"] == "process_owner_lost"
        await restored_connector.dispatch(execute)
        assert marker.read_bytes() == b"x" and not restored_connector.terminal.runs
    finally:
        restored.close()
    await connector.dispatch(command("stdin-input", "input", text="原始输入\n"))
    await asyncio.wait_for(connector.terminal.runs["run"].done.wait(), 15)
    result = (await connector.dispatch(command("stdin-read", "read")))["result"]
    assert "reply:原始输入" in result["output"]
    assert result["completion"]["status"] == "succeeded" and marker.read_bytes() == b"x"
    await connector.terminal.close()


@pytest.fixture
def outsider():
    process = subprocess.Popen(
        [sys.executable, "-c", "import time;time.sleep(60)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )
    try:
        yield process
    finally:
        process.terminate()
        process.wait(timeout=10)


async def test_cancel_owned_descendant_after_parent_exits(local, tmp_path, outsider):
    connector, path = local
    replace(Config.load(path), full_access=True).save(path)
    child = tmp_path / "child.py"
    parent = tmp_path / "parent.py"
    pidfile = tmp_path / "child.pid"
    ticks = tmp_path / "ticks"
    child.write_text(
        "import os,time\nfrom pathlib import Path\n"
        f"Path({str(pidfile)!r}).write_text(str(os.getpid()))\n"
        f"while True:\n with open({str(ticks)!r},'a') as f: f.write('x');f.flush()\n"
        " time.sleep(.04)\n"
    )
    parent.write_text(
        "import subprocess,sys,time\ntime.sleep(.3)\n"
        f"subprocess.Popen([sys.executable,{str(child)!r}])\n"
    )
    await connector.dispatch(
        command("parent", "exec", command=python_command(parent), cwd=str(tmp_path))
    )
    run = connector.terminal.runs["run"]
    await wait_until(lambda: pidfile.exists() and run.process.returncode is not None)
    assert not run.done.is_set()  # descendant still owns the original stdout pipe
    replace(Config.load(path), full_access=False).save(path)
    if sys.platform == "win32":
        interruption = await connector.dispatch(command("interrupt-parent", "interrupt"))
        assert interruption["result"]["error"]["code"] == "interrupt_unsupported"
    result = await asyncio.wait_for(connector.dispatch(command("cancel-parent", "cancel")), 15)
    assert result["result"]["completion"]["status"] == "cancelled"
    length = ticks.stat().st_size
    await asyncio.sleep(0.2)
    assert ticks.stat().st_size == length
    assert outsider.poll() is None
    await connector.terminal.close()


async def test_large_output_rotation_keeps_original_offsets_unicode_and_completion(local, tmp_path):
    connector, path = local
    replace(Config.load(path), full_access=True).save(path)
    # The Unicode character straddles a segment and a read-page boundary.
    original = b"a" * (2 * LOG_SEGMENT_BYTES - 1) + "😀tail\n".encode()
    payload = tmp_path / "large-output.bin"
    payload.write_bytes(original)
    script = tmp_path / "large-output.py"
    marker = tmp_path / "execution-count"
    script.write_text(
        "import sys\nfrom pathlib import Path\n"
        f"with open({str(marker)!r}, 'a') as counter: counter.write('x')\n"
        f"sys.stdout.buffer.write(Path({str(payload)!r}).read_bytes())\n"
        "sys.stdout.buffer.flush()\n",
    )
    execute = command(
        "large-exec", "exec", run="large-run", command=python_command(script), cwd=str(tmp_path)
    )
    await connector.dispatch(execute)
    await asyncio.wait_for(connector.terminal.runs["large-run"].done.wait(), 20)
    segments = connector.terminal._log_segments("large-run")
    assert len(segments) == LOG_SEGMENTS
    assert all(file.stat().st_size <= LOG_SEGMENT_BYTES for _, file in segments)
    assert sum(file.stat().st_size for _, file in segments) <= LOG_SEGMENTS * LOG_SEGMENT_BYTES
    expected = original
    first = connector.terminal.read("large-run", 0)
    assert first["output_lost"] is True
    assert first["output_offset"] == LOG_SEGMENT_BYTES
    cursor, output = first["next_cursor"], first["output"]
    while not first["eof"]:
        first = connector.terminal.read("large-run", cursor)
        assert first["next_cursor"] > cursor
        assert first["output_lost"] is False
        cursor = first["next_cursor"]
        output += first["output"]
    assert output.encode() == expected[LOG_SEGMENT_BYTES:]
    assert cursor == len(expected)
    assert first["completion"]["status"] == "succeeded"
    restored = Journal(connector.journal.path, namespace="test-device")
    try:
        restored_connector = Connector(Config.load(path), restored, config_path=path)
        old = restored_connector.terminal.read("large-run", 0)
        assert old["output_lost"] and old["output_offset"] == LOG_SEGMENT_BYTES
        assert old["completion"] == first["completion"]
        await restored_connector.dispatch(execute)
        assert marker.read_text() == "x" and not restored_connector.terminal.runs
    finally:
        restored.close()
    await connector.terminal.close()
