"""Service ownership and the original job publish in one Manager transaction."""

import os
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from tests.unit.test_terminal_environment import manager_at

from yuki.sandbox import persistent

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX environment runtime")


@pytest.mark.parametrize("failure_stage", ["owner_update", "after_commit"])
async def test_service_publication_is_atomic_across_fault_and_database_reopen(
    tmp_path, monkeypatch, failure_stage
):
    manager = manager_at(tmp_path)
    await manager.service({"action": "register", "name": "original", "command": "sleep 1"})
    with manager.db:
        manager.db.execute("UPDATE environment_services SET enabled=1")
    if failure_stage == "owner_update":
        manager.db.execute(
            "CREATE TRIGGER publication_failure BEFORE UPDATE OF run_id ON environment_services "
            "BEGIN SELECT RAISE(ABORT, 'owner publication fault'); END"
        )
    else:

        class FailedQueue:
            def put_nowait(self, value):
                raise RuntimeError("queue publication interrupted after commit")

        manager.queue = FailedQueue()
    with pytest.raises((sqlite3.IntegrityError, RuntimeError)):
        await manager.restore_services()
    expected = int(failure_stage == "after_commit")
    jobs = manager.db.execute("SELECT id FROM jobs").fetchall()
    service = manager.db.execute("SELECT * FROM environment_services").fetchone()
    assert len(jobs) == manager.db.execute("SELECT COUNT(*) FROM environment_jobs").fetchone()[0]
    assert len(jobs) == expected
    assert service["attempts"] == expected
    assert service["run_id"] == (jobs[0][0] if jobs else None)
    with manager.db:
        manager.db.execute("DROP TRIGGER IF EXISTS publication_failure")
    await manager.close()

    manager = manager_at(tmp_path)
    try:
        if failure_stage == "owner_update":
            await manager.restore_services()
        original = manager.db.execute("SELECT * FROM environment_services").fetchone()
        assert original["run_id"] and original["attempts"] == 1
        if os.name == "posix":
            current = {
                "Id": "original-environment",
                "State": {"Running": True, "StartedAt": "original-start"},
                "HostConfig": {"RestartPolicy": {"Name": "no"}},
                "NetworkSettings": {"Networks": {"egress": {"IPAddress": "172.30.0.2"}}},
            }
            manager.inspect_container = AsyncMock(return_value=current)
            manager.command = AsyncMock(return_value=(0, b""))
            monkeypatch.setattr(
                persistent, "Execd", lambda *_: SimpleNamespace(request=AsyncMock(return_value={}))
            )
            # Reconstruct the worker's original queued run without starting a native process.
            manager.queue = persistent.asyncio.Queue()
            await manager.recover()
            assert manager.queue.get_nowait() == original["run_id"]
            manager.command.assert_not_awaited()
        await manager.restore_services()
        assert manager.db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
        assert manager.db.execute("SELECT COUNT(*) FROM environment_jobs").fetchone()[0] == 1
        restored = manager.db.execute("SELECT * FROM environment_services").fetchone()
        assert restored["run_id"] == original["run_id"] and restored["attempts"] == 1
    finally:
        await manager.close()
