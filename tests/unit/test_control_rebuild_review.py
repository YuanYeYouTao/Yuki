"""Original staged proposals, watermarks, authority and revision conflicts."""

from dataclasses import replace

import pytest
from sqlalchemy import event
from tests.unit import test_memory_rebuild as fixtures
from tests.unit.test_control_plane_foundation import context

from qq_ai_bot.control_plane import (
    ControlCommand,
    ControlCommandError,
    ControlCommandService,
    ControlQueryError,
    ControlQueryService,
    PageRequest,
    ProblemCode,
)
from qq_ai_bot.domain.identity import RequestId
from qq_ai_bot.memory.rebuild.models import MemoryRebuildSelection
from qq_ai_bot.memory.rebuild.worker import MemoryRebuildWorker
from qq_ai_bot.persistence.control_command import ControlCommandAdapter
from qq_ai_bot.persistence.control_query import ControlQueryAdapter


async def test_proposal_pages_content_grants_cas_and_original_commit(database):
    settings, ledger, _, provider, service = await fixtures._service(database)
    for index in range(3):
        await fixtures._event(ledger, message_id=f"review-{index}", content=f"我住在杭州{index}")
    run = await service.plan(MemoryRebuildSelection(all_events=True), actor_user_id="9000")
    await service.start(run.public_id, actor_user_id="9000")
    worker = MemoryRebuildWorker(service, interval_seconds=1)
    await worker.process_once()
    await worker.process_once()
    commands = ControlCommandService(
        ControlCommandAdapter(database, settings=settings, rebuild_service=service)
    )
    queries = ControlQueryService(ControlQueryAdapter(database))
    ctx = context(
        "control.memory.metadata.read", "control.memory.content.read", "control.memory.rebuild"
    )
    rows = await queries.list_memory_rebuild_proposals(
        ctx, PageRequest(limit=2), run_id=run.public_id, include_content=True
    )
    assert len(rows.items) == 2 and rows.next_cursor is not None
    assert rows.items[0].fields["content"] == "我住在杭州0"
    last = await queries.list_memory_rebuild_proposals(
        ctx,
        PageRequest(limit=2, cursor=rows.next_cursor),
        run_id=run.public_id,
        include_content=True,
    )
    assert len(last.items) == 1 and last.next_cursor is None
    with pytest.raises(ControlQueryError):
        await queries.list_memory_rebuild_proposals(
            context("control.memory.metadata.read"),
            PageRequest(cursor=rows.next_cursor),
            run_id=run.public_id,
        )
    sql = []

    def capture(_, __, statement, *args):
        sql.append(statement)

    event.listen(database.engine.sync_engine, "before_cursor_execute", capture)
    try:
        metadata = await queries.list_memory_rebuild_proposals(
            context("control.memory.metadata.read"),
            PageRequest(),
            run_id=run.public_id,
            include_content=True,
        )
    finally:
        event.remove(database.engine.sync_engine, "before_cursor_execute", capture)
    assert not metadata.items[0].fields["content_visible"]
    assert not any("claim_json" in statement for statement in sql)
    original = await queries.read_memory_maintenance_run(ctx, f"rebuild:{run.public_id}")
    revision = original.fields["revision"]
    for action, ids in [
        ("approve", [int(row.resource_id) for row in rows.items]),
        ("reject", [int(last.items[0].resource_id)]),
    ]:
        ctx = replace(ctx, request_id=RequestId.new())
        update = ControlCommand(
            request_id=ctx.request_id,
            expected_revision=revision,
            payload={"action": action, "resource_id": run.public_id, "spec": {"proposal_ids": ids}},
        )
        accepted = await commands.rebuild_memory(ctx, update)
        assert await commands.rebuild_memory(ctx, update) == accepted
        assert accepted.revision > revision
        stale = replace(ctx, request_id=RequestId.new())
        with pytest.raises(ControlCommandError) as conflict:
            await commands.rebuild_memory(
                stale,
                ControlCommand(
                    request_id=stale.request_id,
                    expected_revision=revision,
                    payload={"action": "commit", "resource_id": run.public_id},
                ),
            )
        assert conflict.value.problem.code is ProblemCode.VERSION_CONFLICT
        revision = accepted.revision
    ctx = replace(ctx, request_id=RequestId.new())
    committed = await commands.rebuild_memory(
        ctx,
        ControlCommand(
            request_id=ctx.request_id,
            expected_revision=revision,
            payload={"action": "commit", "resource_id": run.public_id},
        ),
    )
    assert committed.effective_state["status"] == "committing"
    assert provider.requests == 3
    await worker.process_once()
    final = await queries.list_memory_rebuild_proposals(ctx, PageRequest(), run_id=run.public_id)
    assert {row.fields["review_status"] for row in final.items} == {"approved", "rejected"}
    assert provider.requests == 3


async def test_failed_retry_keeps_original_watermarks_and_requires_explicit_resume(database):
    from sqlalchemy import select

    from qq_ai_bot.memory.enums import MemoryRebuildRunStatus
    from qq_ai_bot.persistence.models import MemoryRebuildRunModel

    settings, ledger, _, provider, service = await fixtures._service(database)
    await fixtures._event(ledger, message_id="retry", content="我住在杭州")
    run = await service.plan(MemoryRebuildSelection(all_events=True), actor_user_id="9000")
    async with database.immediate_session() as session:
        row = await session.scalar(
            select(MemoryRebuildRunModel).where(MemoryRebuildRunModel.public_id == run.public_id)
        )
        row.status = MemoryRebuildRunStatus.FAILED.value
        row.scan_checkpoint_event_id = run.snapshot_max_event_id
        row.extraction_requests = 7
    queries = ControlQueryService(ControlQueryAdapter(database))
    ctx = context("control.memory.metadata.read", "control.operation.retry")
    original = await queries.read_memory_maintenance_run(ctx, f"rebuild:{run.public_id}")
    assert original.fields["plan_statistics"]["matched_events"] == 1
    commands = ControlCommandService(
        ControlCommandAdapter(database, settings=settings, rebuild_service=service)
    )
    command = ControlCommand(
        request_id=ctx.request_id,
        expected_revision=original.fields["revision"],
        payload={"action": "retry", "resource_id": f"rebuild:{run.public_id}"},
    )
    result = await commands.retry_operation(ctx, command)
    assert result.effective_state["status"] == "extraction_paused"
    assert await commands.retry_operation(ctx, command) == result
    final = await queries.read_memory_maintenance_run(ctx, f"rebuild:{run.public_id}")
    assert final.fields["extraction_requests"] == 7
    assert final.fields["scan_checkpoint_event_id"] == run.snapshot_max_event_id
    assert provider.requests == 0
