"""Planning scans outside the writer; domain run IDs remain the recovery identity."""

from dataclasses import replace

import pytest
from sqlalchemy import func, select
from tests.conftest import make_settings
from tests.unit.test_control_plane_foundation import context

from qq_ai_bot.control_plane import (
    ControlCommand,
    ControlCommandError,
    ControlCommandService,
    ControlQueryError,
    ControlQueryService,
    OperationKind,
    OperationStatus,
    PageRequest,
    ProblemCode,
)
from qq_ai_bot.domain.identity import RequestId
from qq_ai_bot.identity.db_models import CanonicalPersonModel
from qq_ai_bot.persistence.control_command import ControlCommandAdapter
from qq_ai_bot.persistence.control_query import ControlQueryAdapter
from qq_ai_bot.persistence.event_repository import EventLedgerRepository
from qq_ai_bot.persistence.models import MemoryRebuildRunModel


@pytest.mark.asyncio
async def test_plan_scan_replay_operation_paging_and_no_fabricated_person(database, monkeypatch):
    scans = 0
    async with database.sessions() as session:
        original_person_count = await session.scalar(select(func.count(CanonicalPersonModel.id)))
    original = EventLedgerRepository.count_rebuild_candidates

    async def count(repository, *args, **kwargs):
        nonlocal scans
        scans += 1
        # Opening another SQLite writer would time out if the command held its writer here.
        async with database.immediate_session() as session:
            assert (
                await session.scalar(select(func.count(CanonicalPersonModel.id)))
                == original_person_count
            )
        return await original(repository, *args, **kwargs)

    monkeypatch.setattr(EventLedgerRepository, "count_rebuild_candidates", count)
    commands = ControlCommandService(
        ControlCommandAdapter(
            database, settings=make_settings(database.url, memory_rebuild_enabled=True)
        )
    )
    queries = ControlQueryService(ControlQueryAdapter(database))
    ctx = context("control.memory.rebuild", "control.operation.read", "control.operation.retry")
    command = ControlCommand(
        request_id=ctx.request_id,
        expected_revision=0,
        payload={"action": "plan", "spec": {"maximum_events": 20, "minimum_event_id": 1}},
    )
    first = await commands.rebuild_memory(ctx, command)
    assert await commands.rebuild_memory(ctx, command) == first
    assert scans == 1
    assert first.operation.status is OperationStatus.QUEUED and first.operation.progress is None
    assert (await queries.read_operation(ctx, first.operation.operation_id)) == first.operation
    ctx = replace(ctx, request_id=RequestId.new())
    second = await commands.rebuild_memory(
        ctx,
        ControlCommand(
            request_id=ctx.request_id,
            expected_revision=0,
            payload={"action": "plan", "spec": {"maximum_events": 30}},
        ),
    )
    page = await queries.list_operations(ctx, PageRequest(limit=1), kind=OperationKind.REBUILD)
    assert page.items[0].operation_id == first.operation.operation_id
    assert page.next_cursor is not None
    next_page = await queries.list_operations(
        ctx, PageRequest(limit=1, cursor=page.next_cursor), kind=OperationKind.REBUILD
    )
    assert next_page.items[0].operation_id == second.operation.operation_id
    with pytest.raises(ControlQueryError):
        await queries.list_operations(
            ctx, PageRequest(cursor=page.next_cursor), kind=OperationKind.CONTROL
        )
    ctx = replace(ctx, request_id=RequestId.new())
    with pytest.raises(ControlCommandError) as exc:
        await commands.rebuild_memory(
            ctx,
            ControlCommand(
                request_id=ctx.request_id,
                expected_revision=0,
                payload={"action": "start", "resource_id": "mistyped-run"},
            ),
        )
    assert exc.value.problem.code is ProblemCode.NOT_FOUND
    async with database.sessions() as session:
        assert await session.scalar(select(func.count(MemoryRebuildRunModel.id))) == 2
        assert (
            await session.scalar(select(func.count(CanonicalPersonModel.id)))
            == original_person_count
        )


@pytest.mark.asyncio
async def test_maintenance_is_one_shot_not_a_mutating_plan_and_shares_worker_lock(
    database, monkeypatch
):
    import asyncio

    from qq_ai_bot.memory.maintenance import MemoryMaintenanceWorker
    from qq_ai_bot.memory.repository import MemoryFactRepository
    from qq_ai_bot.memory.service import MemoryFactService

    settings = make_settings(database.url)
    worker = MemoryMaintenanceWorker(
        settings=settings, facts=MemoryFactService(MemoryFactRepository(database))
    )
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def process():
        nonlocal calls
        calls += 1
        # Control reservation has committed; a real domain writer is available.
        async with database.immediate_session():
            pass
        entered.set()
        await release.wait()
        return 3

    monkeypatch.setattr(worker, "_process_once_unlocked", process)
    commands = ControlCommandService(ControlCommandAdapter(database, maintenance=worker))
    ctx = context("control.memory.maintenance")
    for payload in ({"action": "plan"}, {"action": "run", "resource_id": "invented"}):
        ctx = replace(ctx, request_id=RequestId.new())
        with pytest.raises(ControlCommandError) as exc:
            await commands.maintain_memory(
                ctx, ControlCommand(request_id=ctx.request_id, expected_revision=0, payload=payload)
            )
        assert exc.value.problem.code is ProblemCode.VALIDATION_ERROR
    assert calls == 0
    ctx = replace(ctx, request_id=RequestId.new())
    command = ControlCommand(
        request_id=ctx.request_id, expected_revision=0, payload={"action": "run"}
    )
    task = asyncio.create_task(commands.maintain_memory(ctx, command))
    await asyncio.wait_for(entered.wait(), 2)
    queued_worker = asyncio.create_task(worker.process_once())
    try:
        await asyncio.sleep(0)
        assert calls == 1 and not queued_worker.done()
        assert not (await commands.maintain_memory(ctx, command)).success
    finally:
        release.set()
        result = await task
        await queued_worker
    assert result.success and result.revision == 1
    assert await commands.maintain_memory(ctx, command) == result
    assert calls == 2  # one command and one ordinary worker cycle, no replay
