"""Service ownership and the original job publish in one Manager transaction."""

import os
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from tests.unit.test_terminal_environment import manager_at

from yuki.sandbox import persistent

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX environment runtime")


async def test_service_publication_rolls_back_before_commit_and_recovers_original_after_commit(
    tmp_path, monkeypatch
):
    manager = manager_at(tmp_path)
    await manager.service({"action": "register", "name": "original", "command": "sleep 1"})
    with manager.db:
        manager.db.execute("UPDATE environment_services SET enabled=1")
    manager.db.execute(
        "CREATE TRIGGER publication_failure BEFORE UPDATE OF run_id ON environment_services "
        "BEGIN SELECT RAISE(ABORT, 'owner publication fault'); END"
    )
    with pytest.raises(sqlite3.IntegrityError, match="owner publication fault"):
        await manager.restore_services()
    service = manager.db.execute("SELECT * FROM environment_services").fetchone()
    assert not manager.db.execute("SELECT id FROM jobs").fetchall()
    assert not manager.db.execute("SELECT id FROM environment_jobs").fetchall()
    assert service["run_id"] is None and service["attempts"] == 0
    with manager.db:
        manager.db.execute("DROP TRIGGER publication_failure")

    class FailedQueue:
        def put_nowait(self, value):
            raise RuntimeError("queue publication interrupted after commit")

    manager.queue = FailedQueue()
    with pytest.raises(RuntimeError, match="queue publication interrupted after commit"):
        await manager.restore_services()
    original = manager.db.execute("SELECT * FROM environment_services").fetchone()
    identity = original["run_id"]
    assert identity and original["attempts"] == 1
    assert [row[0] for row in manager.db.execute("SELECT id FROM jobs")] == [identity]
    assert [row[0] for row in manager.db.execute("SELECT id FROM environment_jobs")] == [identity]
    await manager.close()

    manager = manager_at(tmp_path)
    try:
        manager.inspect_container = AsyncMock(
            return_value={
                "Id": "original-environment",
                "State": {"Running": True, "StartedAt": "original-start"},
                "HostConfig": {"RestartPolicy": {"Name": "no"}},
                "NetworkSettings": {"Networks": {"egress": {"IPAddress": "172.30.0.2"}}},
            }
        )
        manager.command = AsyncMock(return_value=(0, b""))
        monkeypatch.setattr(
            persistent, "Execd", lambda *_: SimpleNamespace(request=AsyncMock(return_value={}))
        )
        # Recover the original queued run without starting a native process.
        await manager.recover()
        assert manager.queue.get_nowait() == identity
        manager.command.assert_not_awaited()
        await manager.restore_services()
        assert [row[0] for row in manager.db.execute("SELECT id FROM jobs")] == [identity]
        assert [row[0] for row in manager.db.execute("SELECT id FROM environment_jobs")] == [
            identity
        ]
        restored = manager.db.execute("SELECT * FROM environment_services").fetchone()
        assert restored["run_id"] == identity and restored["attempts"] == 1
    finally:
        await manager.close()
