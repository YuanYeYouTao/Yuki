"""Queue failures must not silently terminate the plugin worker."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.exc import OperationalError

from qq_ai_bot.emoji.worker import EmojiWorker
from qq_ai_bot.memory.reflection.worker import MemoryReflectionWorker
from qq_ai_bot.plugin_host.background_turns import PluginBackgroundTurnWorker
from qq_ai_bot.plugin_host.notification_delivery import PluginNotificationOutboxWorker


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_claim", [True, False])
async def test_worker_recovers_claim_and_admission_failures(fail_claim):
    job = SimpleNamespace(id=7, attempts=1)
    repository = SimpleNamespace(claim_turn=AsyncMock(), fail_turn=AsyncMock())
    worker = PluginBackgroundTurnWorker(
        repository=repository,
        ledger=None,
        runtime_config=None,
        chat=None,
        turns=None,
        conversation_scopes=None,
    )
    calls = 0

    async def claim():
        nonlocal calls
        calls += 1
        if calls == 1:
            if fail_claim:
                raise RuntimeError("claim unavailable")
            return job
        worker._stop.set()
        worker.wake()
        return None

    repository.claim_turn.side_effect = claim
    worker._execute = AsyncMock(side_effect=RuntimeError("admission unavailable"))
    await worker.start()
    await asyncio.wait_for(asyncio.shield(worker._task), timeout=4)
    assert calls == 2
    if fail_claim:
        repository.fail_turn.assert_not_called()
    else:
        repository.fail_turn.assert_awaited_once_with(7, attempt=1, error_category="RuntimeError")
    assert (await worker.health())["running"] is False
    await worker.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "worker_type", [PluginNotificationOutboxWorker, EmojiWorker, MemoryReflectionWorker]
)
async def test_durable_workers_resume_after_database_lock(worker_type):
    worker = object.__new__(worker_type)
    worker._stop = asyncio.Event()
    calls = 0

    async def run_queue():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OperationalError("BEGIN IMMEDIATE", {}, RuntimeError("database is locked"))
        worker._stop.set()

    worker._run_queue = run_queue
    await asyncio.wait_for(worker._run(), timeout=4)
    assert calls == 2
