"""Cancellation markers belong to active extraction tasks, never historical runs."""

import asyncio
import gc

import pytest
from tests.unit.test_memory_rebuild import _admin, _event, _service

from yuki.memory.enums import MemoryRebuildRunStatus
from yuki.memory.rebuild.models import MemoryRebuildSelection


@pytest.mark.asyncio
async def test_cancelled_idle_run_is_idempotent_without_active_extraction(database):
    _settings, ledger, _facts, provider, service = await _service(database)
    await _event(ledger, message_id="idle-cancel")
    actor = await _admin(database)
    run = await service.plan(MemoryRebuildSelection(all_events=True), actor_user_id=actor)
    first = await service.cancel(run.public_id, actor_user_id=actor)
    replay = await service.cancel(run.public_id, actor_user_id=actor)
    assert first.public_id == replay.public_id == run.public_id
    assert replay.status is MemoryRebuildRunStatus.CANCELLED
    assert service.active_in_flight_calls == provider.requests == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit_cancel", [True, False])
async def test_extraction_cancellation_preserves_owner_and_releases_task(database, explicit_cancel):
    _settings, ledger, _facts, _provider, service = await _service(database)
    await _event(ledger, message_id="cancel-lifecycle")
    run = await service.plan(
        MemoryRebuildSelection(all_events=True), actor_user_id=await _admin(database)
    )
    await service.start(run.public_id, actor_user_id=await _admin(database))
    run = await service.repository.get_run(run.public_id)
    entered = asyncio.Event()

    async def blocked(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    service.extractor.extract = blocked
    execution = asyncio.create_task(service.process_extraction_once(run))
    await asyncio.wait_for(entered.wait(), 5)
    if explicit_cancel:
        await service.cancel(run.public_id, actor_user_id=await _admin(database))
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
