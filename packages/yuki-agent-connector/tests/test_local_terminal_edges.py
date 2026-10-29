"""Original process-tree cancellation and incremental Unicode output boundaries."""

import asyncio
import json
import sqlite3
from types import SimpleNamespace

from yuki_agent_connector.terminal import LocalRun, LocalTerminal


def test_running_log_keeps_partial_utf8_bytes_for_the_next_read(tmp_path):
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE operations(id TEXT,command TEXT)")
    db.execute(
        "INSERT INTO operations VALUES (?,?)",
        (
            "original-request",
            json.dumps({"resource": "terminal", "action": "exec", "run_id": "run"}),
        ),
    )
    journal = SimpleNamespace(
        db=db,
        operation=lambda key: {"status": "running", "pending": True},
    )
    terminal = LocalTerminal(tmp_path, journal, None)
    path = tmp_path / "run.log"
    try:
        path.write_bytes("😀".encode()[:1])
        first = terminal.read("run", 0)
        assert first["output"] == "" and first["next_cursor"] == 0
        with path.open("ab") as stream:
            stream.write("😀".encode()[1:])
        second = terminal.read("run", first["next_cursor"])
        assert second["output"] == "😀" and second["next_cursor"] == 4
    finally:
        db.close()


def test_cancel_still_targets_the_original_job_after_its_parent_exits(tmp_path):
    cancelled = []
    job = SimpleNamespace(cancel=lambda: cancelled.append("original-job"))
    run = LocalRun(
        "original-request",
        "original-run",
        SimpleNamespace(returncode=0),
        tmp_path / "output.log",
        asyncio.Event(),
        job=job,
    )
    LocalTerminal._cancel(run)
    assert cancelled == ["original-job"]
