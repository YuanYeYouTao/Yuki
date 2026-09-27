"""Domain effects, terminal tasks, and command/query projections stay consistent."""

from dataclasses import replace

import pytest
from sqlalchemy import func, select
from tests.conftest import make_settings
from tests.support.social_identity_cases import social_env
from tests.unit.test_control_automation_authority import automation_service, group_script
from tests.unit.test_control_plane_foundation import context
from tests.unit.test_memory_dream import _empty_dream_statistics

from qq_ai_bot.automation.models import AutomationStatus
from qq_ai_bot.automation.repository import AutomationRepository
from qq_ai_bot.control_plane import (
    ControlCommand,
    ControlCommandError,
    ControlCommandService,
    ControlQueryService,
    OperationKind,
    OperationStatus,
    PageRequest,
    ProblemCode,
)
from qq_ai_bot.conversation.canonical_db_models import ControlCommandReceiptModel
from qq_ai_bot.domain.identity import RequestId
from qq_ai_bot.memory.dream.db_models import MemoryDreamRunModel
from qq_ai_bot.memory.dream.models import DreamRunMode
from qq_ai_bot.memory.dream.repository import DreamRepository
from qq_ai_bot.memory.rebuild.repository import MemoryRebuildRepository
from qq_ai_bot.persistence.control_command import ControlCommandAdapter
from qq_ai_bot.persistence.control_query import ControlQueryAdapter
from qq_ai_bot.persistence.models import (
    AdminOperationEventModel,
    AutomationModel,
    MemoryRebuildRunModel,
)
from qq_ai_bot.persistence.unit_of_work import next_updated_at, state_revision


async def setup_automation(database, tmp_path):
    env = await social_env(database, tmp_path)
    service = automation_service(database)
    ctx = context("control.automation.mutate")
    ctx = replace(ctx, principal=replace(ctx.principal, person_id=None))
    commands = ControlCommandService(ControlCommandAdapter(database, automation=service))
    command = ControlCommand(
        request_id=ctx.request_id,
        expected_revision=0,
        payload={
            "action": "create",
            "spec": {
                "script": group_script(),
                "owner_id": "self",
                "conversation_id": env.context.conversation_id,
            },
        },
    )
    return commands, ctx, command


@pytest.mark.asyncio
@pytest.mark.parametrize("domain", ["automation", "rebuild"])
@pytest.mark.parametrize("error_type", [RuntimeError, ValueError])
async def test_failed_domain_command_rolls_back_partial_create_and_replays_original_failure(
    database,
    tmp_path,
    monkeypatch,
    domain,
    error_type,
):
    calls = 0
    if domain == "automation":
        commands, ctx, command = await setup_automation(database, tmp_path)
        repository, method, model = AutomationRepository, "create", AutomationModel
        invoke = commands.mutate_automation
    else:
        commands = ControlCommandService(
            ControlCommandAdapter(
                database,
                settings=make_settings(database.url, memory_rebuild_enabled=True),
            )
        )
        ctx = context("control.memory.rebuild")
        command = ControlCommand(
            request_id=ctx.request_id, expected_revision=0, payload={"action": "plan"}
        )
        repository, method, model = MemoryRebuildRepository, "create_run", MemoryRebuildRunModel
        invoke = commands.rebuild_memory
    original = getattr(repository, method)

    async def fail_after_create(self, *args, **kwargs):
        nonlocal calls
        calls += 1
        await original(self, *args, **kwargs)
        raise error_type("injected after real domain create")

    monkeypatch.setattr(repository, method, fail_after_create)
    for _ in range(2):
        with pytest.raises(ControlCommandError):
            await invoke(ctx, command)
        async with database.sessions() as session:
            assert await session.scalar(select(func.count()).select_from(model)) == 0
            receipts = (await session.scalars(select(ControlCommandReceiptModel))).all()
            assert len(receipts) == 1 and receipts[0].status == "failed"
            assert receipts[0].request_id == ctx.request_id.text
            audits = (await session.scalars(select(AdminOperationEventModel))).all()
            assert len(audits) == 1 and not audits[0].success
    assert calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", [AutomationStatus.CANCELLED, AutomationStatus.COMPLETED])
@pytest.mark.parametrize("action", ["pause", "resume", "run_now"])
async def test_terminal_automation_is_neither_resurrected_nor_reported_as_changed(
    database,
    tmp_path,
    terminal,
    action,
):
    commands, ctx, create = await setup_automation(database, tmp_path)
    result = await commands.mutate_automation(ctx, create)
    task_id = int(result.resource_id)
    async with database.immediate_session() as session:
        row = await session.get(AutomationModel, task_id)
        row.status, row.next_run_at = terminal.value, None
        row.updated_at = next_updated_at(row.updated_at)
        revision = state_revision(row.updated_at)
    ctx = replace(ctx, request_id=RequestId.new())
    command = ControlCommand(
        request_id=ctx.request_id,
        expected_revision=revision,
        payload={"action": action, "resource_id": result.resource_id},
    )
    with pytest.raises(ControlCommandError) as exc:
        await commands.mutate_automation(ctx, command)
    assert exc.value.problem.code is ProblemCode.PRECONDITION_FAILED
    stored = await AutomationRepository(database).get(task_id)
    assert stored.status is terminal and stored.next_run_at is None
    assert state_revision(stored.updated_at) == revision


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["rebuild", "dream"])
@pytest.mark.parametrize("previous_error", [None, "cluster_failed"])
async def test_cancel_operation_has_same_terminal_projection_on_command_replay_and_query(
    database, kind, previous_error
):
    commands = ControlCommandService(
        ControlCommandAdapter(
            database,
            settings=make_settings(database.url, memory_rebuild_enabled=True),
        )
    )
    ctx = context(f"control.memory.{kind}", "control.operation.read")
    if kind == "rebuild":
        created = await commands.rebuild_memory(
            ctx,
            ControlCommand(
                request_id=ctx.request_id,
                expected_revision=0,
                payload={"action": "plan"},
            ),
        )
        public_id, revision = created.resource_id, created.revision
        invoke = commands.rebuild_memory
    else:
        run = await DreamRepository(database).create_run(
            mode=DreamRunMode.FULL,
            statistics=_empty_dream_statistics(),
            clusters=(),
            snapshot_max_fact_id=0,
            actor_user_id=None,
            scheduled_slot=None,
        )
        public_id, revision = run.public_id, state_revision(run.updated_at)
        invoke = commands.dream_memory
    model = MemoryRebuildRunModel if kind == "rebuild" else MemoryDreamRunModel
    if previous_error is not None:
        async with database.immediate_session() as session:
            row = await session.scalar(select(model).where(model.public_id == public_id))
            row.status = "extraction_paused" if kind == "rebuild" else "running"
            row.error_category = previous_error
            row.updated_at = next_updated_at(row.updated_at)
            revision = state_revision(row.updated_at)
    ctx = replace(ctx, request_id=RequestId.new())
    command = ControlCommand(
        request_id=ctx.request_id,
        expected_revision=revision,
        payload={"action": "cancel", "resource_id": public_id},
    )
    cancelled = await invoke(ctx, command)
    assert cancelled.operation.status is OperationStatus.CANCELLED
    assert cancelled.operation.progress == 1.0
    assert cancelled.operation.error_category is None
    assert (await invoke(ctx, command)).operation == cancelled.operation
    queries = ControlQueryService(ControlQueryAdapter(database))
    assert await queries.read_operation(ctx, f"{kind}:{public_id}") == cancelled.operation
    if kind == "dream" and previous_error is not None:
        async with database.sessions() as session:
            row = await session.scalar(select(model).where(model.public_id == public_id))
            assert row.error_category == previous_error  # projection never rewrites domain evidence


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["rebuild", "dream"])
@pytest.mark.parametrize("error", [None, "provider_timeout", "private/path: raw diagnostic"])
async def test_failed_operation_with_missing_or_unsafe_diagnostic_remains_queryable(
    database, kind, error
):
    commands = ControlCommandService(
        ControlCommandAdapter(
            database,
            settings=make_settings(database.url, memory_rebuild_enabled=True),
        )
    )
    ctx = context("control.memory.rebuild", "control.operation.read")
    if kind == "rebuild":
        result = await commands.rebuild_memory(
            ctx,
            ControlCommand(
                request_id=ctx.request_id,
                expected_revision=0,
                payload={"action": "plan"},
            ),
        )
        public_id, model, status = result.resource_id, MemoryRebuildRunModel, "failed"
    else:
        run = await DreamRepository(database).create_run(
            mode=DreamRunMode.FULL,
            statistics=_empty_dream_statistics(),
            clusters=(),
            snapshot_max_fact_id=0,
            actor_user_id=None,
            scheduled_slot=None,
        )
        public_id, model, status = run.public_id, MemoryDreamRunModel, "partial_failed"
    async with database.immediate_session() as session:
        row = await session.scalar(select(model).where(model.public_id == public_id))
        row.status, row.error_category = status, error
        row.updated_at = next_updated_at(row.updated_at)
    queries = ControlQueryService(ControlQueryAdapter(database))
    operation = await queries.read_operation(ctx, f"{kind}:{public_id}")
    assert operation.status is OperationStatus.FAILED and operation.progress == 1.0
    assert operation.error_category == (
        ("rebuild_failed" if kind == "rebuild" else "partial_failed")
        if error is None
        else "provider_timeout"
        if error == "provider_timeout"
        else "operation_failed"
    )
    page = await queries.list_operations(ctx, PageRequest(), kind=OperationKind(kind))
    assert page.items == (operation,)
    async with database.sessions() as session:
        row = await session.scalar(select(model).where(model.public_id == public_id))
        assert row.error_category == error
