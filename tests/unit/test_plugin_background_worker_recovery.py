"""Queue failures must not silently terminate the plugin worker."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from qq_ai_bot.plugin_host.background_turns import PluginBackgroundTurnWorker


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
