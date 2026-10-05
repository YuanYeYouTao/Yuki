"""Short-state reads neither reserve a writer nor erase expired version facts."""

import asyncio
import sqlite3

import pytest

from qq_ai_bot.domain.messages import ChatMessage
from qq_ai_bot.workspace.short_state import ShortState
from qq_ai_bot.workspace.store import WorkspaceError, WorkspaceStore


@pytest.mark.parametrize("contents", ["missing", "live", "expired"])
async def test_initialized_manifest_snapshot_is_read_only_with_another_writer(
    tmp_path, monkeypatch, contents
):
    store = WorkspaceStore(tmp_path / "workspace")
    state = ShortState(store)
    # Workspace's first schema initialization is allowed; the cold case here
    # has an initialized manifest but has never created the short_state table.
    with store._transaction(write=False):
        pass
    if contents != "missing":
        state.update({"slot": 1, "text": "original", "expected_revision": 0})
        with store._transaction() as db:
            db.execute(
                "UPDATE short_state SET expires_at=?", (1000 if contents == "expired" else 2000,)
            )
    now_calls = []

    def now():
        now_calls.append(True)
        return 1000

    monkeypatch.setattr("qq_ai_bot.workspace.short_state.time.time", now)
    connect = sqlite3.connect
    statements = []

    def traced(*args, **kwargs):
        db = connect(*args, **kwargs)
        db.set_trace_callback(statements.append)
        return db

    with store._transaction() as writer:
        monkeypatch.setattr("qq_ai_bot.workspace.store.sqlite3.connect", traced)
        rows = await asyncio.wait_for(asyncio.to_thread(state.snapshot), 1)
        if contents == "missing":
            assert rows == []
            assert (
                writer.execute("SELECT 1 FROM sqlite_master WHERE name='short_state'").fetchone()
                is None
            )
            assert not now_calls
        else:
            assert rows == [
                {
                    "slot": 1,
                    "text": "" if contents == "expired" else "original",
                    "revision": 1,
                    "expires_at": 1000 if contents == "expired" else 2000,
                }
            ]
            stored = dict(writer.execute("SELECT * FROM short_state").fetchone())
            assert stored["text"] == "original"
            assert stored["revision"] == 1
            assert stored["expires_at"] == rows[0]["expires_at"]
            assert len(now_calls) == 1
    assert statements
    assert any(sql == "BEGIN" for sql in statements)
    assert all(sql.lstrip().upper().startswith(("SELECT", "BEGIN", "COMMIT")) for sql in statements)
    assert "BEGIN IMMEDIATE" not in statements


async def test_expired_snapshot_preserves_cas_and_conflict_receipt_hides_old_text(
    tmp_path, monkeypatch
):
    state = ShortState(WorkspaceStore(tmp_path / "workspace"))
    state.update({"slot": 1, "text": "expired-secret", "expected_revision": 0})
    with state.store._transaction() as db:
        db.execute("UPDATE short_state SET expires_at=1000")
    monkeypatch.setattr("qq_ai_bot.workspace.short_state.time.time", lambda: 1000)
    expected = [{"slot": 1, "text": "", "revision": 1, "expires_at": 1000}]
    assert state.snapshot() == expected
    messages = (ChatMessage("user", "current"),)
    assert await state.inject(messages) == messages
    rejected = state.update({"slot": 1, "text": "stale-resurrection", "expected_revision": 0})
    assert rejected == {"ok": False, "error": "revision_conflict", "records": expected}
    with state.store._transaction(write=False) as db:
        assert db.execute("SELECT text FROM short_state").fetchone()[0] == ""
    accepted = state.update({"slot": 1, "text": "new", "expected_revision": 1})
    assert accepted["ok"]
    assert accepted["records"] == [{"slot": 1, "text": "new", "revision": 2, "expires_at": 87400}]


def test_failed_capacity_write_rolls_back_cleanup_but_reads_still_hide_expired_text(
    tmp_path, monkeypatch
):
    state = ShortState(WorkspaceStore(tmp_path / "workspace"))
    state.update({"slot": 1, "text": "expired", "expected_revision": 0})
    with state.store._transaction() as db:
        db.execute("UPDATE short_state SET expires_at=1000")
    monkeypatch.setattr("qq_ai_bot.workspace.short_state.time.time", lambda: 1000)
    with pytest.raises(WorkspaceError, match="short_state_capacity_exceeded"):
        state.update({"slot": 2, "text": "测" * 300, "expected_revision": 0})
    with state.store._transaction(write=False) as db:
        stored = [dict(row) for row in db.execute("SELECT * FROM short_state")]
    assert stored == [{"slot": 1, "text": "expired", "revision": 1, "expires_at": 1000}]
    assert state.snapshot() == [{"slot": 1, "text": "", "revision": 1, "expires_at": 1000}]
