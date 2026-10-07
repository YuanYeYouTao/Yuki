"""Cancellation markers belong to active extraction tasks, never historical runs."""

import asyncio
import gc
from types import SimpleNamespace

import pytest
from tests.unit.test_memory_rebuild import _event, _service

from qq_ai_bot.memory.enums import MemoryRebuildRunStatus
from qq_ai_bot.memory.rebuild.models import MemoryRebuildSelection
from qq_ai_bot.memory.rebuild.service import MemoryRebuildService


@pytest.mark.asyncio
async def test_cancelled_idle_runs_do_not_accumulate_process_state():
    async def transition(*args, **kwargs):
        return True

    async def require(run_id, **kwargs):
        return SimpleNamespace(status=MemoryRebuildRunStatus.PLANNED, public_id=run_id)

    service = MemoryRebuildService(
        settings=None,
        repository=SimpleNamespace(transition=transition),
        ledger=None,
        extractor=None,
        processor=None,
    )
    service._require = require
    for index in range(10000):
        await service.cancel(str(index), actor_user_id="test", authorize=False)
    assert not service._cancelled_tasks
    assert not service._in_flight_tasks


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit_cancel", [True, False])
async def test_extraction_cancellation_preserves_owner_and_releases_task(database, explicit_cancel):
    _settings, ledger, _facts, _provider, service = await _service(database)
    await _event(ledger, message_id="cancel-lifecycle")
    run = await service.plan(MemoryRebuildSelection(all_events=True), actor_user_id="9000")
    await service.start(run.public_id, actor_user_id="9000")
    run = await service.repository.get_run(run.public_id)
    entered = asyncio.Event()

    async def blocked(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    service.extractor.extract = blocked
    execution = asyncio.create_task(service.process_extraction_once(run))
    await asyncio.wait_for(entered.wait(), 5)
    if explicit_cancel:
        await service.cancel(run.public_id, actor_user_id="9000")
        assert await execution == 0
        assert (
            await service.repository.get_run(run.public_id)
        ).status is MemoryRebuildRunStatus.CANCELLED
    else:
        execution.cancel()
        with pytest.raises(asyncio.CancelledError):
            await execution
    assert service.active_in_flight_calls == 0
    assert not service._in_flight_tasks
    del execution
    await asyncio.sleep(0)
    gc.collect()
    assert not service._cancelled_tasks
