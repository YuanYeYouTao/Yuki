"""Driver codes control recovery; a lock-shaped message cannot authorize replay."""

import json
import sqlite3

import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from tests.unit.test_runtime_recovery import setup

from qq_ai_bot.runtime.activation_outcome import ExitReason, classify_failure
from qq_ai_bot.runtime.work_recovery_schema import recovery
from qq_ai_bot.runtime.work_supervisor import recover_failure


@pytest.mark.parametrize(
    ("code", "expected", "retryable"),
    [
        (sqlite3.SQLITE_BUSY, "sqlite_busy", True),
        (sqlite3.SQLITE_BUSY_SNAPSHOT, "sqlite_busy", True),
        (sqlite3.SQLITE_LOCKED, "sqlite_locked", False),
        (sqlite3.SQLITE_LOCKED_SHAREDCACHE, "sqlite_locked", False),
        (sqlite3.SQLITE_IOERR, "database_failure", False),
        (None, "database_failure", False),
    ],
)
def test_error_text_never_substitutes_for_driver_code(code, expected, retryable):
    original = sqlite3.OperationalError("database is locked: private data")
    if code is not None:
        original.sqlite_errorcode = code
    wrapped = OperationalError("private SQL", {"private": "parameter"}, original)
    for exc in (original, wrapped):
        failure = classify_failure(exc, "commit")
        assert (failure.code, failure.stage, failure.retryable) == (expected, "commit", retryable)
        assert "private" not in str(failure.diagnostics)


def test_real_wal_writer_busy_and_snapshot_conflict_are_distinct_from_locked(tmp_path):
    path = tmp_path / "contention.sqlite3"
    first = sqlite3.connect(path, timeout=0)
    second = sqlite3.connect(path, timeout=0)
    try:
        first.execute("PRAGMA journal_mode=WAL")
        first.execute("CREATE TABLE evidence (id INTEGER PRIMARY KEY, value INTEGER)")
        first.execute("INSERT INTO evidence VALUES (1, 0)")
        first.commit()
        first.execute("BEGIN IMMEDIATE")
        with pytest.raises(sqlite3.OperationalError) as busy:
            second.execute("UPDATE evidence SET value=1 WHERE id=1")
        assert busy.value.sqlite_errorcode == sqlite3.SQLITE_BUSY
        assert classify_failure(busy.value).retryable
        second.rollback()
        first.rollback()

        first.execute("BEGIN")
        assert first.execute("SELECT value FROM evidence WHERE id=1").fetchone() == (0,)
        second.execute("UPDATE evidence SET value=2 WHERE id=1")
        second.commit()
        with pytest.raises(sqlite3.OperationalError) as snapshot:
            first.execute("UPDATE evidence SET value=3 WHERE id=1")
        assert snapshot.value.sqlite_errorcode == sqlite3.SQLITE_BUSY_SNAPSHOT
        assert classify_failure(snapshot.value).diagnostics == {
            "sqlite_errorcode": sqlite3.SQLITE_BUSY_SNAPSHOT
        }
        first.rollback()
        # Only a new transaction can see and update the current database plan.
        first.execute("UPDATE evidence SET value=3 WHERE id=1 AND value=2")
        first.commit()
        assert second.execute("SELECT value FROM evidence WHERE id=1").fetchone() == (3,)
    finally:
        first.close()
        second.close()


@pytest.mark.parametrize("code", [sqlite3.SQLITE_LOCKED, sqlite3.SQLITE_LOCKED_SHAREDCACHE])
async def test_locked_fails_original_work_without_resetting_paid_budget(database, tmp_path, code):
    control = await setup(database, tmp_path)
    original_id = control.current["id"]
    await control.repository.checkpoint(control.lease, original_id, None, models=1)
    control.current = await control.repository.get(original_id)
    await control.session.save("dispatched")
    original = sqlite3.OperationalError("database table is locked")
    original.sqlite_errorcode = code
    outcome = await recover_failure(control, OperationalError("UPDATE", {}, original))
    assert outcome.reason is ExitReason.FAILED
    assert control.current["id"] == original_id
    assert control.current["state"] == "failed"
    assert control.current["model_requests"] == 1
    async with database.sessions() as session:
        row = (
            await session.execute(
                select(recovery.c.failure_json, recovery.c.not_before).where(
                    recovery.c.work_id == original_id
                )
            )
        ).one()
    assert json.loads(row[0])["code"] == "sqlite_locked"
    assert row[1] == 0
