"""A run and its first resume cursor are one admission boundary."""

import asyncio
import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from tests.conftest import make_settings
from tests.unit.test_automation_runtime import FakeClock, _inbound, _router, _script

from qq_ai_bot.automation.executor import AutomationExecutor
from qq_ai_bot.automation.models import AutomationScript, AutomationStatus, RunStatus
from qq_ai_bot.automation.registry import CapabilityResult, build_capability_registry
from qq_ai_bot.automation.repository import AutomationRepository
from qq_ai_bot.automation.service import AutomationService
from qq_ai_bot.automation.work_cursor import load as load_cursor
from qq_ai_bot.automation.worker import AutomationWorker
from qq_ai_bot.domain.tool_actor import ToolActor
from qq_ai_bot.persistence.models import AutomationModel, AutomationRunModel
from qq_ai_bot.runtime.automation_budget_schema import budgets
from qq_ai_bot.runtime.work_recovery_schema import invocations
from qq_ai_bot.time.service import TimeContextService


async def setup_case(database, *, agent=False):
    clock = FakeClock(datetime(2026, 9, 22, tzinfo=UTC))
    settings = make_settings(database.url, automation_enabled=True, runtime_work_enabled=True)
    repository = AutomationRepository(database)
    time_service = TimeContextService(database, clock=clock)
    sends = []

    async def send(arguments, context):
        sends.append(context.automation_run_id)
        return CapabilityResult(data={"sent": True}, messages_sent=1)

    registry = build_capability_registry({"social.send_message": send, "yuki.agent": send})
    service = AutomationService(
        settings=settings,
        repository=repository,
        registry=registry,
        time_service=time_service,
    )
    script = _script()
    if agent:
        raw = script.model_dump(mode="json")
        raw["steps"] = [
            {
                "id": "work",
                "call": "yuki.agent",
                "arguments": {"instruction": "finish", "context_profile": "none"},
            }
        ]
        raw["limits"].update(agent_budget_managed=True, max_llm_calls=1)
        script = AutomationScript.model_validate(raw)
    row = await service.create(
        script,
        actor=ToolActor.from_inbound(_inbound()),
        conversation_key="private:10001",
    )
    clock.advance(2)
    return SimpleNamespace(
        clock=clock,
        settings=settings,
        repository=repository,
        time=time_service,
        registry=registry,
        row=row,
        sends=sends,
    )


async def admit(case):
    return await case.repository.create_run(
        case.row.id,
        scheduled_for=case.row.next_run_at,
        actual_started_at=case.clock.now(),
    )


@pytest.mark.asyncio
async def test_initial_cursor_failure_rolls_back_run_and_duplicate_admission_preserves_hash(
    database,
    monkeypatch,
):
    case = await setup_case(database)
    execute = AsyncSession.execute

    async def fail_cursor(session, statement, *args, **kwargs):
        if str(statement).startswith("INSERT INTO runtime_automation_cursors"):
            raise RuntimeError("injected_initial_cursor_failure")
        return await execute(session, statement, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(AsyncSession, "execute", fail_cursor)
        with pytest.raises(RuntimeError, match="injected_initial_cursor_failure"):
            await admit(case)
    async with database.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(AutomationRunModel)) == 0
        assert await session.scalar(select(func.count()).select_from(invocations)) == 0

    admitted = await asyncio.gather(admit(case), admit(case))
    assert sum(run is not None for run in admitted) == 1
    run = next(run for run in admitted if run is not None)
    assert await load_cursor(database, run.id, case.row.script_hash) == ("ready", {"next_step": 0})
    async with database.sessions() as session, session.begin():
        await session.execute(
            update(AutomationModel)
            .where(AutomationModel.id == case.row.id)
            .values(
                script_hash="b" * 64,
            )
        )
    assert await admit(case) is None
    assert await load_cursor(database, run.id, case.row.script_hash) == ("ready", {"next_step": 0})
    assert await load_cursor(database, run.id, "b" * 64) == ("changed", {"next_step": 0})


@pytest.mark.asyncio
async def test_restart_before_first_step_resumes_original_admitted_run(database):
    case = await setup_case(database)
    original = await admit(case)
    # The process stops immediately after admission, without dispatching a step.
    # A late restart must recover this run instead of treating it as a fresh misfire.
    case.clock.advance(case.row.misfire_grace_seconds + 1)
    repository = AutomationRepository(database)
    recovered = await repository.resumable_run(case.row.id, case.row.next_run_at)
    assert recovered.id == original.id
    claimed = await repository.claim_due(
        worker_id="restarted", now=case.clock.now(), lease_seconds=30
    )
    worker = AutomationWorker(
        settings=case.settings,
        repository=repository,
        time_service=case.time,
        executor=AutomationExecutor(
            settings=case.settings,
            registry=case.registry,
            repository=repository,
            time_service=case.time,
            router=_router(),
        ),
    )
    await worker._process(claimed[0])
    assert case.sends == [original.id]
    history = await repository.run_history(case.row.id)
    assert len(history) == 1
    assert history[0].id == original.id
    assert history[0].status is RunStatus.SUCCEEDED
    assert (await repository.get(case.row.id)).status is AutomationStatus.COMPLETED


@pytest.mark.asyncio
async def test_legacy_orphan_is_reconciled_without_replay_or_usage_reset(database):
    case = await setup_case(database)
    original = await admit(case)
    async with database.sessions() as session, session.begin():
        await session.execute(delete(invocations).where(invocations.c.run_id == original.id))
        await session.execute(
            update(AutomationRunModel)
            .where(AutomationRunModel.id == original.id)
            .values(
                steps_completed=1,
                llm_calls=7,
                tool_calls=9,
                messages_sent=2,
                result_summary_json='{"existing_evidence":"retained"}',
            )
        )
        await session.execute(insert(budgets).values(run_id=original.id, models=23, tools=47))
    case.clock.advance(case.row.misfire_grace_seconds + 1)
    repository = AutomationRepository(database)
    recovered = await repository.resumable_run(case.row.id, case.row.next_run_at)
    assert recovered.id == original.id
    claimed = await repository.claim_due(
        worker_id="legacy-recovery", now=case.clock.now(), lease_seconds=30
    )
    worker = AutomationWorker(
        settings=case.settings,
        repository=repository,
        time_service=case.time,
        executor=AutomationExecutor(
            settings=case.settings,
            registry=case.registry,
            repository=repository,
            time_service=case.time,
            router=_router(),
        ),
    )
    await worker._process(claimed[0])
    assert not case.sends
    history = await repository.run_history(case.row.id)
    assert len(history) == 1 and history[0].id == original.id
    assert history[0].status is RunStatus.UNCERTAIN
    assert history[0].error_category == "missing_initial_run_cursor"
    assert (
        history[0].steps_completed,
        history[0].llm_calls,
        history[0].tool_calls,
        history[0].messages_sent,
    ) == (1, 7, 9, 2)
    assert history[0].result_summary == {"existing_evidence": "retained"}
    async with database.sessions() as session:
        assert (await session.execute(select(budgets.c.models, budgets.c.tools))).one() == (23, 47)
        assert not (await session.execute(select(invocations))).first()
    assert (await repository.get(case.row.id)).status is AutomationStatus.FAILED


@pytest.mark.parametrize(
    ("rejection", "phase", "category"),
    [
        ("inactive", None, "automation_inactive"),
        ("route", "agent", "disconnected"),
        ("runtime", "agent", "automation_runtime_required"),
        ("dispatching", "dispatching", "step_outcome_requires_reconciliation"),
        ("changed", "agent", "step_outcome_requires_reconciliation"),
    ],
)
async def test_recovery_preflight_rejection_retains_cursor_and_run_usage(
    database, rejection, phase, category
):
    case = await setup_case(database, agent=rejection == "runtime")
    run = await admit(case)
    async with database.sessions() as session, session.begin():
        await session.execute(
            update(AutomationRunModel)
            .where(AutomationRunModel.id == run.id)
            .values(
                steps_completed=1,
                llm_calls=7,
                tool_calls=9,
                messages_sent=2,
                result_summary_json='{"existing_evidence":"retained"}',
            )
        )
        await session.execute(insert(budgets).values(run_id=run.id, models=23, tools=47))
        if phase is None:
            await session.execute(delete(invocations).where(invocations.c.run_id == run.id))
        else:
            await session.execute(
                update(invocations)
                .where(invocations.c.run_id == run.id)
                .values(
                    phase=phase,
                    script_hash="b" * 64 if rejection == "changed" else case.row.script_hash,
                    payload_json=json.dumps(
                        {
                            "next_step": 0,
                            "steps_completed": 1,
                            "llm_calls": 11,
                            "tool_calls": 13,
                            "messages_sent": 3,
                            "work_id": "existing-work",
                        }
                    ),
                )
            )
    (claimed,) = await case.repository.claim_due(
        worker_id="recovery", now=case.clock.now(), lease_seconds=30
    )
    if rejection == "inactive":
        async with database.sessions() as session, session.begin():
            await session.execute(
                update(AutomationModel)
                .where(AutomationModel.id == case.row.id)
                .values(status=AutomationStatus.PAUSED.value)
            )
    settings = case.settings.model_copy(update={"runtime_work_enabled": rejection != "runtime"})
    worker = AutomationWorker(
        settings=settings,
        repository=case.repository,
        time_service=case.time,
        executor=AutomationExecutor(
            settings=settings,
            registry=case.registry,
            repository=case.repository,
            time_service=case.time,
            router=_router("disconnected" if rejection == "route" else None),
        ),
    )
    await worker._process(claimed)
    (recorded,) = await case.repository.run_history(case.row.id)
    assert recorded.error_category == category
    assert recorded.status in {RunStatus.BLOCKED, RunStatus.UNCERTAIN}
    assert (
        recorded.steps_completed,
        recorded.llm_calls,
        recorded.tool_calls,
        recorded.messages_sent,
    ) == ((1, 7, 9, 2) if phase is None else (1, 11, 13, 3))
    assert recorded.result_summary["existing_evidence"] == "retained"
    assert not case.sends
    async with database.sessions() as session:
        assert (await session.execute(select(budgets.c.models, budgets.c.tools))).one() == (23, 47)


async def test_late_claimant_cannot_adopt_replacement_owner(database):
    case = await setup_case(database)
    run = await admit(case)
    (old,) = await case.repository.claim_due(
        worker_id="old-owner", now=case.clock.now(), lease_seconds=30
    )
    case.clock.advance(31)
    (current,) = await case.repository.claim_due(
        worker_id="current-owner", now=case.clock.now(), lease_seconds=30
    )
    executor = AutomationExecutor(
        settings=case.settings,
        registry=case.registry,
        repository=case.repository,
        time_service=case.time,
        router=_router(),
    )
    result = await executor.execute(old, run)
    assert result.error_category == "automation_lease_lost"
    assert not case.sends
    assert await load_cursor(database, run.id, case.row.script_hash) == ("ready", {"next_step": 0})
    assert (await case.repository.get(case.row.id)).claimed_by == current.claimed_by
    worker = AutomationWorker(
        settings=case.settings,
        repository=case.repository,
        time_service=case.time,
        executor=executor,
    )
    await worker._process(old)
    assert not case.sends
    assert (await case.repository.run_history(case.row.id))[0].status is RunStatus.RUNNING
    assert (await case.repository.get(case.row.id)).claimed_by == current.claimed_by
    await worker._process(current)
    assert case.sends == [run.id]
    assert (await case.repository.run_history(case.row.id))[0].status is RunStatus.SUCCEEDED
