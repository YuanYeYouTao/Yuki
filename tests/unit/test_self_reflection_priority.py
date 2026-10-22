"""Reflection waits for chat and resumes original batches only from real cycles."""

import asyncio
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select
from tests.conftest import make_settings
from tests.unit.test_memory_v2 import _append_event

from yuki.memory.self_reflection.repository import SelfReflectionRepository
from yuki.memory.self_reflection.service import _CHECKPOINT, ReflectionCheckpoint
from yuki.memory.self_reflection.worker import SelfReflectionWorker
from yuki.model_runtime.executor import BackgroundModelPreempted
from yuki.persistence.models import (
    MemorySelfReflectionCycleModel as Cycle,
)
from yuki.persistence.models import (
    MemorySelfReflectionRunModel as Run,
)
from yuki.persistence.repositories import EventLedgerRepository

LOCAL = ZoneInfo("Asia/Shanghai")
SCHEDULED = datetime(2026, 10, 22, 20, tzinfo=LOCAL)
OFF_HOURS = datetime(2026, 10, 22, 21, tzinfo=LOCAL)


async def reflection_worker(database, *, owners=1):
    settings = make_settings(
        database.url,
        memory_self_reflection_event_threshold=1,
        memory_self_reflection_schedule_hours="4,12,20",
    )
    repository = SelfReflectionRepository(database)
    await repository.scan_new_events()
    ledger = EventLedgerRepository(database)
    events = [
        await _append_event(ledger, message_id=f"priority-{i}", user_id=str(1001 + i))
        for i in range(owners)
    ]
    service = SimpleNamespace(wait_for_quiet=AsyncMock(), reflect=AsyncMock(return_value=(0, 0)))
    worker = SelfReflectionWorker(settings=settings, repository=repository, service=service)
    return worker, repository, service, events


async def test_preemption_stops_cycle_before_claiming_another_owner(database):
    worker, _, service, _ = await reflection_worker(database, owners=2)
    service.reflect.side_effect = BackgroundModelPreempted("foreground arrived")

    assert await worker.process_once(SCHEDULED) == 0

    async with database.read_session() as reader:
        runs = list(await reader.scalars(select(Run)))
        assert len(runs) == 1
        original = runs[0]
        assert (original.status, original.error_category, original.retry_state) == (
            "failed",
            "preempted",
            "waiting",
        )
        assert original.attempt_count == 0
        cycle = await reader.get(Cycle, original.cycle_id)
        report = await worker.control.get(cycle.id)
    assert report["attempted_batches"] == report["deferred_batches"] == 1
    assert report["failed_batches"] == 0
    assert service.reflect.await_args.args[0].run_id == original.id


async def test_off_hour_waiting_batch_keeps_original_id_until_manual_cycle(database):
    worker, _, service, events = await reflection_worker(database)
    service.reflect.side_effect = BackgroundModelPreempted("foreground arrived")
    await worker.process_once(SCHEDULED)
    original_run_id = service.reflect.await_args.args[0].run_id
    async with database.read_session() as reader:
        original_cycle_id = await reader.scalar(select(Cycle.id))

    service.reflect.side_effect = None
    assert await worker.process_once(OFF_HOURS) == 0
    assert service.reflect.await_count == 1
    async with database.read_session() as reader:
        assert list(await reader.scalars(select(Cycle.id))) == [original_cycle_id]
        run = await reader.get(Run, original_run_id)
        assert (run.status, run.retry_state) == ("failed", "waiting")

    manual = await worker.run_now(
        source_event_id=events[0].id,
        conversation_id=events[0].canonical_conversation_id,
    )
    assert await worker.process_once(OFF_HOURS) == 1
    assert service.reflect.await_args.args[0].run_id == original_run_id
    async with database.read_session() as reader:
        run = await reader.get(Run, original_run_id)
        assert run.status == "completed"
        assert run.cycle_id == manual["id"]
        assert await reader.get(Cycle, original_cycle_id) is not None
        assert {cycle.trigger for cycle in await reader.scalars(select(Cycle))} == {
            "scheduled",
            "manual",
        }


async def test_schedule_is_saved_but_no_batch_preparation_runs_before_quiet(database, monkeypatch):
    worker, repository, service, _ = await reflection_worker(database)
    waiting, quiet = asyncio.Event(), asyncio.Event()

    async def wait_for_quiet():
        waiting.set()
        await quiet.wait()

    service.wait_for_quiet.side_effect = wait_for_quiet
    scan = AsyncMock(wraps=repository.scan_new_events)
    claim = AsyncMock(wraps=repository.claim_due)
    monkeypatch.setattr(repository, "scan_new_events", scan)
    monkeypatch.setattr(repository, "claim_due", claim)
    task = asyncio.create_task(worker.process_once(SCHEDULED))
    try:
        await asyncio.wait_for(waiting.wait(), 2)
        scan.assert_not_awaited()
        claim.assert_not_awaited()
        service.reflect.assert_not_awaited()
        async with database.read_session() as reader:
            cycle = await reader.scalar(select(Cycle))
            assert (cycle.source_key, cycle.status) == ("2026-10-22:20", "running")
        quiet.set()
        assert await asyncio.wait_for(task, 5) == 1
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_no_cycle_keeps_polling_before_the_next_scheduled_hour(database, monkeypatch):
    worker, repository, service, _ = await reflection_worker(database)
    service.wait_for_quiet.side_effect = AssertionError("no cycle should wait for quiet")
    scan = AsyncMock(wraps=repository.scan_new_events)
    monkeypatch.setattr(repository, "scan_new_events", scan)

    before_schedule = datetime(2026, 10, 22, 11, 59, tzinfo=LOCAL)
    assert await asyncio.wait_for(worker.process_once(before_schedule), 1) == 0
    service.wait_for_quiet.assert_not_awaited()
    scan.assert_not_awaited()
    service.reflect.assert_not_awaited()

    service.wait_for_quiet.side_effect = None
    scheduled = datetime(2026, 10, 22, 12, tzinfo=LOCAL)
    assert await worker.process_once(scheduled) == 1
    async with database.read_session() as reader:
        cycle = await reader.scalar(select(Cycle))
        assert (cycle.source_key, cycle.status) == ("2026-10-22:12", "completed")


async def test_restart_finishes_preempted_cycle_without_claiming_another_batch(
    database, monkeypatch
):
    worker, repository, service, _ = await reflection_worker(database, owners=2)
    service.reflect.side_effect = BackgroundModelPreempted("foreground arrived")
    monkeypatch.setattr(worker.control, "finish", AsyncMock(side_effect=asyncio.CancelledError))
    with pytest.raises(asyncio.CancelledError):
        await worker.process_once(SCHEDULED)
    original_run_id = service.reflect.await_args.args[0].run_id
    async with database.read_session() as reader:
        original = await reader.get(Run, original_run_id)
        cycle_id = original.cycle_id
        assert (await reader.get(Cycle, cycle_id)).status == "running"

    resumed_service = SimpleNamespace(
        wait_for_quiet=AsyncMock(), reflect=AsyncMock(return_value=(0, 0))
    )
    resumed = SelfReflectionWorker(
        settings=worker._settings, repository=repository, service=resumed_service
    )
    claim = AsyncMock(wraps=repository.claim_due)
    monkeypatch.setattr(repository, "claim_due", claim)
    assert await resumed.process_once(OFF_HOURS) == 0
    claim.assert_not_awaited()
    resumed_service.reflect.assert_not_awaited()
    async with database.read_session() as reader:
        assert list(await reader.scalars(select(Run.id))) == [original_run_id]
        original = await reader.get(Run, original_run_id)
        assert (original.status, original.retry_state) == ("failed", "waiting")
        assert (await reader.get(Cycle, cycle_id)).status == "completed"


async def test_old_retry_cycle_retires_without_consuming_checkpoint_or_new_owner(database):
    worker, repository, service, _ = await reflection_worker(database, owners=2)
    service.reflect.side_effect = BackgroundModelPreempted("foreground arrived")
    await worker.process_once(SCHEDULED)
    original_run_id = service.reflect.await_args.args[0].run_id
    saved = _CHECKPOINT.dump_json(
        ReflectionCheckpoint({"proposals": [], "episodes": []}, {}, {})
    ).decode()
    await repository.save_checkpoint(original_run_id, saved)
    legacy = await worker.control.enqueue(trigger="retry", key="legacy-queued-retry")
    async with database.immediate_session() as writer:
        original = await writer.get(Run, original_run_id)
        original.cycle_id = legacy["id"]
        original.error_category = "interrupted"

    service.reflect.side_effect = None
    assert await worker.process_once(OFF_HOURS) == 0
    assert service.reflect.await_count == 1
    async with database.read_session() as reader:
        assert list(await reader.scalars(select(Run.id))) == [original_run_id]
        original = await reader.get(Run, original_run_id)
        assert (original.status, original.retry_state, original.checkpoint_json) == (
            "failed",
            "waiting",
            saved,
        )
        cycle = await reader.get(Cycle, legacy["id"])
        assert cycle.completed_at is not None
        assert cycle.trigger == "retry"


async def test_close_cancels_quiet_wait_without_holding_writer(database):
    worker, _, service, events = await reflection_worker(database)
    waiting = asyncio.Event()

    async def quiet():
        waiting.set()
        await asyncio.Event().wait()

    service.wait_for_quiet.side_effect = quiet
    await worker.start()
    await worker.run_now(
        source_event_id=events[0].id, conversation_id=events[0].canonical_conversation_id
    )
    await asyncio.wait_for(waiting.wait(), 2)
    async with database.immediate_session():
        service.reflect.assert_not_awaited()
    task = worker._task
    await asyncio.wait_for(worker.close(), 2)
    assert task.cancelled() and worker._task is None
