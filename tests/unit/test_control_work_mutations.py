"""Individual Work management preserves execution identity and late receipts."""

import asyncio
import json

import pytest
from sqlalchemy import select, update
from tests.unit import test_control_work_details as work_fixtures
from tests.unit.test_control_plane_foundation import context

from qq_ai_bot.control_plane import (
    ControlCommand,
    ControlCommandError,
    ControlCommandService,
    ProblemCode,
)
from qq_ai_bot.persistence.control_command import ControlCommandAdapter
from qq_ai_bot.runtime.activation_outcome import ExitReason, WorkActivationHandled
from qq_ai_bot.runtime.work_activation import activate_work
from qq_ai_bot.runtime.work_management import manage_work
from qq_ai_bot.runtime.work_recovery_schema import deliveries, recovery
from qq_ai_bot.runtime.work_repository import WorkConflict, WorkRepository
from qq_ai_bot.runtime.work_schema_v1 import effects, inputs, journal, work
from qq_ai_bot.runtime.work_wait_schema import waits

detailed_work = work_fixtures.detailed_work


async def test_child_cancel_preserves_parent_and_reports_actual_child_state(
    database, detailed_work
):
    identity, child = detailed_work
    ctx = context("control.work.mutate")
    service = ControlCommandService(ControlCommandAdapter(database))
    result = await service.mutate_work(ctx, command(ctx, child))
    assert result.effective_state["status"] == "cancelled"
    repo = WorkRepository(database)
    parent = await repo.get(identity)
    assert parent["state"] == "queued" and parent["model_requests"] == 7
    async with database.sessions() as session:
        row = (
            (
                await session.execute(
                    select(inputs).where(inputs.c.source_key == f"worker-result:{child}:2")
                )
            )
            .mappings()
            .one()
        )
    assert row["work_id"] == identity and row["event_id"] is None
    assert json.loads(json.loads(row["payload_json"])["text"])["state"] == "cancelled"


def command(ctx, identity, revision=1, action="cancel", **extra):
    return ControlCommand(
        request_id=ctx.request_id,
        expected_revision=revision,
        payload={"resource_id": identity, "action": action, **extra},
    )


async def test_cancel_only_selected_tree_retains_budget_and_late_receipts(database, detailed_work):
    identity, child = detailed_work
    repository = WorkRepository(database)
    original = await repository.get(identity)
    lease = await repository.acquire(original["conversation_id"], 1)
    sibling = await repository.accept(lease, source_key="sibling", source={}, goal="another goal")
    await repository.enqueue(
        lease.conversation_id, 1, "pending-child", kind="control", work_id=child
    )
    service = ControlCommandService(ControlCommandAdapter(database))
    ctx = context("control.work.mutate")
    cmd = command(ctx, identity)
    first = await service.mutate_work(ctx, cmd)
    assert await service.mutate_work(ctx, cmd) == first
    assert first.effective_state["status"] == "cancelled" and first.revision == 2
    assert (await repository.get(child))["state"] == "cancelled"
    assert (await repository.get(sibling["id"])) == sibling
    assert await repository.valid(lease)
    cancelled = await repository.get(identity)
    assert cancelled["checkpoint_json"] == original["checkpoint_json"]
    assert cancelled["model_requests"] == 7 and cancelled["tool_calls"] == 8
    with pytest.raises(WorkConflict):
        await repository.prepare_effect(lease, identity, "after-cancel", "tool")
    await repository.record_effect("original-effect", "accepted", {"confirmed": True})
    async with database.sessions() as session:
        assert (
            await session.scalar(
                select(effects.c.state).where(effects.c.effect_key == "original-effect")
            )
            == "accepted"
        )
        assert await session.scalar(select(deliveries.c.state)) == "unknown"
        assert await session.scalar(select(waits.c.status)) == "cancelled"
        assert (
            await session.scalar(
                select(inputs.c.state).where(inputs.c.source_key == "pending-child")
            )
            == "cancelled"
        )


async def test_resume_original_chain_preserves_budget_source_and_recovery(database, detailed_work):
    identity, _ = detailed_work
    async with database.immediate_session() as session:
        await session.execute(
            update(work)
            .where(work.c.id == identity)
            .values(
                state="suspended",
                source_json='{"origin":"user_message","private":"original-authority"}',
            )
        )
        await session.execute(update(effects).values(state="accepted"))
        await session.execute(update(deliveries).values(state="accepted"))
        await session.execute(update(waits).values(status="cancelled"))
    repo = WorkRepository(database)
    original = await repo.get(identity)
    async with database.sessions() as session:
        original_journal = (await session.execute(select(journal))).mappings().one()
        original_recovery = (await session.execute(select(recovery))).mappings().one()
    ctx = context("control.work.mutate")
    service = ControlCommandService(ControlCommandAdapter(database))
    cmd = command(ctx, identity, action="resume")
    result = await service.mutate_work(ctx, cmd)
    assert result.resource_id == identity and result.effective_state["status"] == "queued"
    assert await service.mutate_work(ctx, cmd) == result
    queued = await repo.get(identity)
    for field in (
        "id",
        "source_key",
        "source_json",
        "generation",
        "checkpoint_json",
        "model_requests",
        "tool_calls",
        "active_seconds",
    ):
        assert queued[field] == original[field]
    async with database.sessions() as session:
        assert (await session.execute(select(journal))).mappings().one() == original_journal
        assert (await session.execute(select(recovery))).mappings().one() == original_recovery
        assert len((await session.execute(select(work))).all()) == 2


@pytest.mark.parametrize(
    "block",
    [
        "unknown_effect",
        "unknown_delivery",
        "active_wait",
        "generation",
        "lease",
        "unsupported_owner",
        "missing_journal",
    ],
)
async def test_resume_rejects_unresolved_or_obsolete_work(database, detailed_work, block):
    identity, _ = detailed_work
    async with database.immediate_session() as session:
        await session.execute(
            update(work)
            .where(work.c.id == identity)
            .values(state="suspended", source_json='{"origin":"user_message"}')
        )
        if block != "unknown_effect":
            await session.execute(update(effects).values(state="accepted"))
        if block != "unknown_delivery":
            await session.execute(update(deliveries).values(state="accepted"))
        if block != "active_wait":
            await session.execute(update(waits).values(status="cancelled"))
        if block == "generation":
            await session.execute(update(work).where(work.c.id == identity).values(generation=99))
        if block == "unsupported_owner":
            await session.execute(
                update(work)
                .where(work.c.id == identity)
                .values(source_json='{"origin":"scheduled_automation"}')
            )
        if block == "missing_journal":
            from sqlalchemy import delete

            await session.execute(delete(journal))
    repo = WorkRepository(database)
    original = await repo.get(identity)
    if block == "lease":
        await repo.acquire(original["conversation_id"], 1)
    ctx = context("control.work.mutate")
    service = ControlCommandService(ControlCommandAdapter(database))
    cmd = command(ctx, identity, action="resume")
    with pytest.raises(ControlCommandError) as exc:
        await service.mutate_work(ctx, cmd)
    assert exc.value.problem.code in {
        ProblemCode.PRECONDITION_FAILED,
        ProblemCode.STATE_MISMATCH,
        ProblemCode.OPERATION_UNAVAILABLE,
    }
    assert await repo.get(identity) == original
    with pytest.raises(ControlCommandError):
        await service.mutate_work(ctx, cmd)


async def test_concurrent_cancellation_is_cas_and_authorization_is_independent(
    database, detailed_work
):
    identity, _ = detailed_work
    service = ControlCommandService(ControlCommandAdapter(database))
    denied = context("control.execution.metadata.read")
    with pytest.raises(ControlCommandError) as exc:
        await service.mutate_work(denied, command(denied, identity))
    assert exc.value.problem.code is ProblemCode.CAPABILITY_DENIED
    a, b = context("control.work.mutate"), context("control.work.mutate")
    results = await asyncio.gather(
        service.mutate_work(a, command(a, identity)),
        service.mutate_work(b, command(b, identity)),
        return_exceptions=True,
    )
    assert sum(isinstance(result, ControlCommandError) for result in results) == 1
    failed = next(result for result in results if isinstance(result, ControlCommandError))
    assert failed.problem.code is ProblemCode.VERSION_CONFLICT
    assert (await WorkRepository(database).get(identity))["revision"] == 2


async def test_cancelled_activation_exits_without_failure_notice(database, detailed_work):
    identity, _ = detailed_work
    repo = WorkRepository(database)
    item = await repo.get(identity)
    # Select the retained original Work under its original source, no QQ/model.
    source = json.loads(item["source_json"])

    async def validate():
        pass

    with pytest.raises(WorkActivationHandled):
        async with activate_work(
            repo, item["conversation_id"], 1, item["source_key"], source, validate, work_id=identity
        ) as control:
            async with database.immediate_session() as session:
                await manage_work(session, identity, control.current["revision"], "cancel")
            await repo.prepare_effect(control.lease, identity, "late-effect", "tool")
    assert control.outcome.reason is ExitReason.CANCELLED
    async with database.sessions() as session:
        assert (
            await session.scalar(select(deliveries.c.id).where(deliveries.c.kind == "notice"))
            is None
        )


@pytest.mark.parametrize(
    "identity", ["123", "platform:123", "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA", ""]
)
async def test_invalid_id_and_arbitrary_spec_do_not_rebuild_work(database, identity):
    ctx = context("control.work.mutate")
    service = ControlCommandService(ControlCommandAdapter(database))
    with pytest.raises(ControlCommandError) as exc:
        await service.mutate_work(ctx, command(ctx, identity))
    assert exc.value.problem.code is ProblemCode.VALIDATION_ERROR
