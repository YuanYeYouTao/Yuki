"""Hard-kill a real child process at each dispatch window; recover from the DB only.

The fake downstream writes its own append-only log, independent of the Bot DB,
so "not resent" is proven by downstream facts rather than by Bot rows.
"""

import asyncio
import json
import sys
from dataclasses import asdict
from pathlib import Path

import pytest
from tests.integration.test_invocation_crash_windows import facts, prepared

from qq_ai_bot.runtime.work_repository import WorkLease, WorkRepository

CHILD = r"""
import asyncio, json, os, sys
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.runtime.work_repository import WorkLease, WorkRepository

url, lease_json, identity, key, log, stop = sys.argv[1:]

async def main():
    repository = WorkRepository(Database(url))
    lease = WorkLease(**json.loads(lease_json))
    if stop == "after_t1":
        os._exit(9)
    assert await repository.admit_dispatch(lease, identity, key)
    if stop == "after_t2":
        os._exit(9)
    with open(log, "a") as downstream:
        downstream.write(key + "\n")
        downstream.flush()
        os.fsync(downstream.fileno())
    if stop == "after_send":
        os._exit(9)
    await repository.record_effect(
        key, "accepted",
        {"result": '{"ok":true}', "outcome": {"ok": True, "side_effecting": True}},
    )
    os._exit(9)  # after T3: no graceful shutdown either

asyncio.run(main())
"""


async def kill_at(database, lease: WorkLease, identity: str, key: str, log: Path, stop: str):
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        CHILD,
        database.url,
        json.dumps(asdict(lease)),
        identity,
        key,
        str(log),
        stop,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await process.communicate()
    assert process.returncode == 9, stderr.decode()


def downstream(log: Path) -> list[str]:
    return log.read_text().splitlines() if log.exists() else []


@pytest.mark.parametrize(
    ("stop", "dispatched", "sent", "settled"),
    [
        ("after_t1", False, 0, False),
        ("after_t2", True, 0, False),
        ("after_send", True, 1, False),
        ("after_t3", True, 1, True),
    ],
)
async def test_hard_kill_window_recovers_without_resend_or_recharge(
    database, tmp_path, stop, dispatched, sent, settled
):
    owner, invocation = await prepared(database, tmp_path)
    lease, identity = owner.control.lease, owner.control.current["id"]
    key = invocation.identity.operation_id
    log = tmp_path / "downstream.log"
    await kill_at(database, lease, identity, key, log, stop)

    # A fresh repository/journal is the restarted process.
    restarted = WorkRepository(database)
    receipt, total, root = await facts(database, key)
    assert receipt["invocation"]["dispatch_started"] is dispatched
    assert len(downstream(log)) == sent
    assert total == (1 if dispatched else 0)
    assert root == (1 if dispatched else None)
    result = json.loads(await owner.journal.effect_result(key))
    if settled:
        assert result == {"ok": True}
    elif dispatched:
        # T2 committed: possibly sent. Never "not executed", never replayed.
        assert result["uncertain"] is True and result["replay_forbidden"] is True
    if dispatched:
        assert not await restarted.admit_dispatch(lease, identity, key)
    else:
        # Only an undispatched intent may still be admitted, exactly once.
        assert await restarted.admit_dispatch(lease, identity, key)
        assert not await restarted.admit_dispatch(lease, identity, key)
    _, total_after, _ = await facts(database, key)
    assert total_after == 1
    assert len(downstream(log)) == sent
