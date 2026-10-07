"""Outer DSL deadlines preserve original dispatch evidence and effect certainty."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from tests.conftest import make_settings
from tests.integration.test_automation_unified_delivery import sent, setup_run
from tests.unit.test_automation_runtime import FakeClock, _inbound, _router, _script

from qq_ai_bot.automation.executor import AutomationExecutor
from qq_ai_bot.automation.models import AutomationScript, RiskClass, RunStatus
from qq_ai_bot.automation.registry import CapabilityResult, build_capability_registry
from qq_ai_bot.automation.repository import AutomationRepository
from qq_ai_bot.automation.service import AutomationService
from qq_ai_bot.automation.work_cursor import load as load_cursor
from qq_ai_bot.automation.work_cursor import save as save_cursor
from qq_ai_bot.domain.tool_actor import ToolActor
from qq_ai_bot.social.db_models import SocialOperationModel
from qq_ai_bot.time.service import TimeContextService


async def short_remaining_deadline(case, database):
    phase, payload = await load_cursor(database, case.run.id, case.row.script_hash)
    await save_cursor(
        database,
        case.run.id,
        case.row.script_hash,
        phase,
        {**payload, "active_seconds": case.row.script.limits.timeout_seconds - 0.2},
    )


def dispatch_driven_deadline(monkeypatch):
    """Use a real asyncio Timeout, expired at the tested boundary rather than
    during unrelated SQLite setup under load. Certainty assertions stay intact.
    """
    original_timeout = asyncio.timeout
    outer = None

    def controlled_timeout(delay):
        nonlocal outer
        if outer is None:
            outer = original_timeout(None)
            return outer
        return original_timeout(delay)

    def expire():
        assert outer is not None
        outer.reschedule(asyncio.get_running_loop().time())

    monkeypatch.setattr(asyncio, "timeout", controlled_timeout)
    return expire


@pytest.mark.asyncio
async def test_transport_deadline_preserves_uncertain_receipt_and_original_dispatch(
    database, tmp_path, monkeypatch
):
    case = await setup_run(database, tmp_path, strategy="static")
    expire = dispatch_driven_deadline(monkeypatch)
    original_call = case.env.bot.call_api
    calls = 0

    async def delayed(action, **params):
        nonlocal calls
        if action in {"send_group_msg", "send_private_msg"}:
            calls += 1
            expire()
            await asyncio.Event().wait()
        return await original_call(action, **params)

    monkeypatch.setattr(case.env.bot, "call_api", delayed)
    result = await case.executor.execute(case.row, case.run)
    assert result.status is RunStatus.UNCERTAIN and result.error_category == "runtime_timeout"
    async with database.sessions() as session:
        receipt = await session.scalar(select(SocialOperationModel))
        assert receipt.status == "uncertain"
        assert receipt.tool_call_id == case.row.script.steps[0].id
        original_id = receipt.id
    phase, cursor = await load_cursor(database, case.run.id, case.row.script_hash)
    assert phase == "dispatching" and cursor["next_step"] == 0
    replay = await case.executor.execute(case.row, case.run)
    assert replay.status is RunStatus.UNCERTAIN and calls == 1
    async with database.sessions() as session:
        assert await session.scalar(select(SocialOperationModel.id)) == original_id


@pytest.mark.asyncio
async def test_known_acceptance_survives_step_recording_failure_without_resend(
    database, tmp_path, monkeypatch
):
    case = await setup_run(database, tmp_path, strategy="static")
    monkeypatch.setattr(
        case.repository, "record_step", AsyncMock(side_effect=RuntimeError("audit fixture"))
    )
    result = await case.executor.execute(case.row, case.run)
    assert result.status is RunStatus.FAILED and result.error_category == "step_recording_failed"
    assert result.messages_sent == 1 and result.tool_calls == 1
    assert result.summary["effect_status"] == "succeeded"
    async with database.sessions() as session:
        receipt = await session.scalar(select(SocialOperationModel))
        assert receipt.status == "succeeded" and receipt.id == result.summary["effect_operation_id"]
    replay = await case.executor.execute(case.row, case.run)
    assert replay.status is RunStatus.UNCERTAIN and len(sent(case.env)) == 1


async def test_pending_work_checkpoint_failure_counts_completed_usage_once(
    database, tmp_path, monkeypatch
):
    from qq_ai_bot.automation import work_cursor

    case = await setup_run(database, tmp_path, strategy="agentic")
    calls = 0

    async def pending_agent(arguments, context):
        nonlocal calls
        calls += 1
        return CapabilityResult(data={}, llm_calls=1, tool_calls=1, pending_work_id="original-work")

    definition = case.executor._registry.require("yuki.agent")
    case.executor._registry.unregister(definition.name)
    case.executor._registry.register(replace(definition, handler=pending_agent))
    original = work_cursor.save

    async def reject_pending_checkpoint(database, run_id, script_hash, phase, payload, **kwargs):
        if payload.get("work_id") == "original-work":
            raise RuntimeError("checkpoint unavailable")
        return await original(database, run_id, script_hash, phase, payload, **kwargs)

    monkeypatch.setattr(work_cursor, "save", reject_pending_checkpoint)
    result = await case.executor.execute(case.row, case.run)
    assert result.status is RunStatus.FAILED and result.error_category == "step_recording_failed"
    assert result.llm_calls == result.tool_calls == 1
    phase, cursor = await load_cursor(database, case.run.id, case.row.script_hash)
    assert phase == "agent" and cursor["next_step"] == 0 and calls == 1


@pytest.mark.asyncio
async def test_accepted_transport_with_unconfirmed_local_receipt_stays_uncertain(
    database, tmp_path, monkeypatch
):
    case = await setup_run(database, tmp_path, strategy="static")
    monkeypatch.setattr(
        case.env.service.writer, "append", AsyncMock(side_effect=RuntimeError("ledger fixture"))
    )
    result = await case.executor.execute(case.row, case.run)
    assert result.status is RunStatus.UNCERTAIN
    async with database.sessions() as session:
        receipt = await session.scalar(select(SocialOperationModel))
        assert receipt.status == "uncertain"
    await case.executor.execute(case.row, case.run)
    assert len(sent(case.env)) == 1


@pytest.mark.asyncio
async def test_authority_validation_deadline_before_dispatch_is_failed(
    database, tmp_path, monkeypatch
):
    case = await setup_run(database, tmp_path, strategy="static")
    await short_remaining_deadline(case, database)
    original_begin = case.executor._begin_execution
    checks = 0

    async def delayed_validation(*args, **kwargs):
        nonlocal checks
        checks += 1
        if checks > 1:
            await asyncio.Event().wait()
        return await original_begin(*args, **kwargs)

    monkeypatch.setattr(case.executor, "_begin_execution", delayed_validation)
    result = await case.executor.execute(case.row, case.run)
    assert result.status is RunStatus.FAILED and result.error_category == "runtime_timeout"
    assert not sent(case.env)
    async with database.sessions() as session:
        assert await session.scalar(select(SocialOperationModel.id)) is None


@pytest.mark.asyncio
async def test_read_deadline_remains_failed(database):
    async def delayed_read(arguments, context):
        await asyncio.Event().wait()

    clock = FakeClock(datetime.now(UTC))
    settings = make_settings(database.url, automation_enabled=True)
    registry = build_capability_registry({"web.search": delayed_read})
    repository = AutomationRepository(database)
    time_service = TimeContextService(database, clock=clock)
    service = AutomationService(
        settings=settings, repository=repository, registry=registry, time_service=time_service
    )
    raw = _script().model_dump(mode="json")
    raw["steps"] = [{"id": "read", "call": "web.search", "arguments": {"query": "test"}}]
    raw["limits"].update(max_messages=0)
    row = await service.create(
        AutomationScript.model_validate(raw),
        actor=ToolActor.from_inbound(_inbound()),
        conversation_key="private:10001",
    )
    run = await repository.create_run(
        row.id, scheduled_for=row.next_run_at, actual_started_at=clock.now()
    )
    phase, payload = await load_cursor(database, run.id, row.script_hash)
    await save_cursor(database, run.id, row.script_hash, phase, {**payload, "active_seconds": 29.8})
    result = await AutomationExecutor(
        settings=settings,
        registry=registry,
        repository=repository,
        time_service=time_service,
        router=_router(),
    ).execute(row, run)
    assert result.status is RunStatus.FAILED and result.error_category == "runtime_timeout"


@pytest.mark.asyncio
@pytest.mark.parametrize("risk", [RiskClass.SEND, RiskClass.MUTATE])
async def test_untyped_failure_after_effect_dispatch_is_uncertain_and_not_replayed(
    database, tmp_path, monkeypatch, risk
):
    case = await setup_run(database, tmp_path, strategy="static")
    calls = 0

    async def unknown_handler(arguments, context):
        nonlocal calls
        calls += 1
        raise RuntimeError("effect outcome fixture")

    definition = case.executor._registry.require("social.send_message")
    case.executor._registry.unregister(definition.name)
    case.executor._registry.register(replace(definition, handler=unknown_handler, risk_class=risk))
    result = await case.executor.execute(case.row, case.run)
    assert (
        result.status is RunStatus.UNCERTAIN
        and result.error_category == "capability_execution_failed"
    )
    replay = await case.executor.execute(case.row, case.run)
    assert replay.status is RunStatus.UNCERTAIN and calls == 1
