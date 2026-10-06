"""Retired capabilities stop execution without rewriting historical outcome facts.

The historical MCP handler is an in-process fixture; no MCP server or transport
is started. Admission, dispatch, cursor, budget and worker recovery are real.
"""

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from sqlalchemy import insert, select, update
from tests.conftest import make_settings
from tests.unit.test_automation_runtime import FakeClock, _inbound, _router, _script

from qq_ai_bot.automation.executor import AutomationExecutionError, AutomationExecutor
from qq_ai_bot.automation.models import AutomationScript, AutomationStatus, RiskClass, RunStatus
from qq_ai_bot.automation.registry import CapabilityResult, build_capability_registry
from qq_ai_bot.automation.repository import AutomationRepository
from qq_ai_bot.automation.service import AutomationService
from qq_ai_bot.automation.worker import AutomationWorker
from qq_ai_bot.domain.tool_actor import ToolActor
from qq_ai_bot.persistence.models import AutomationRunModel, AutomationStepRunModel
from qq_ai_bot.runtime.automation_budget_schema import budgets
from qq_ai_bot.runtime.work_recovery_schema import invocations
from qq_ai_bot.time.service import TimeContextService

_RETIRED_CALL = "mcp.retired_fixture.write"


@pytest.mark.parametrize(
    ("outcome", "expected_step_status", "expected_phase"),
    [
        ("dispatching", None, "dispatching"),
        ("accepted", "succeeded", "ready"),
        ("uncertain", "uncertain", "dispatching"),
        ("not_dispatched", "failed", "dispatching"),
    ],
)
async def test_retired_capability_recovery_preserves_original_outcome_and_budget(
    database, outcome, expected_step_status, expected_phase
):
    clock = FakeClock(datetime(2026, 10, 7, tzinfo=UTC))
    settings = make_settings(database.url, automation_enabled=True, runtime_work_enabled=True)
    repository = AutomationRepository(database)
    time_service = TimeContextService(database, clock=clock)
    registry = build_capability_registry()
    calls = []

    async def historical_handler(arguments, context):
        calls.append((context.automation_id, context.automation_run_id, context.step_id))
        if outcome == "dispatching":
            # Process interruption after the durable dispatch checkpoint.
            raise asyncio.CancelledError
        if outcome == "uncertain":
            raise AutomationExecutionError("mcp_call_uncertain", uncertain=True, tool_calls=1)
        if outcome == "not_dispatched":
            # A known remote admission rejection is not an unknown effect.
            raise AutomationExecutionError("mcp_request_not_dispatched", tool_calls=1)
        return CapabilityResult(data={"accepted": True, "original_receipt": "fixture-receipt"})

    registry.register(
        replace(
            registry.require("social.send_message"),
            name=_RETIRED_CALL,
            risk_class=RiskClass.MUTATE,
            handler=historical_handler,
        )
    )
    script_json = _script().model_dump(mode="json")
    script_json["steps"][0]["call"] = _RETIRED_CALL
    script_json["limits"]["agent_budget_managed"] = True
    service = AutomationService(
        settings=settings, repository=repository, registry=registry, time_service=time_service
    )
    row = await service.create(
        AutomationScript.model_validate(script_json),
        actor=ToolActor.from_inbound(_inbound()),
        conversation_key="private:10001",
    )
    clock.advance(2)
    (claimed,) = await repository.claim_due(
        worker_id="before-retirement", now=clock.now(), lease_seconds=30
    )
    run = await repository.create_run(
        row.id, scheduled_for=row.next_run_at, actual_started_at=clock.now()
    )
    assert run is not None
    async with database.sessions() as session, session.begin():
        await session.execute(insert(budgets).values(run_id=run.id, models=2, tools=3))
    executor = AutomationExecutor(
        settings=settings,
        registry=registry,
        repository=repository,
        time_service=time_service,
        router=_router(),
    )
    if outcome == "dispatching":
        with pytest.raises(asyncio.CancelledError):
            await executor.execute(claimed, run)
    else:
        first = await executor.execute(claimed, run)
        assert (
            first.status
            is {
                "accepted": RunStatus.SUCCEEDED,
                "uncertain": RunStatus.UNCERTAIN,
                "not_dispatched": RunStatus.FAILED,
            }[outcome]
        )

    # The process did not finish the admitted run. Keep committed accounting
    # independent of whether its last remote outcome was known or unknown.
    async with database.sessions() as session, session.begin():
        await session.execute(
            update(AutomationRunModel)
            .where(AutomationRunModel.id == run.id)
            .values(
                steps_completed=int(outcome == "accepted"),
                tool_calls=1,
                result_summary_json='{"previous_evidence":"retained"}',
            )
        )
        original_cursor = (
            (await session.execute(select(invocations).where(invocations.c.run_id == run.id)))
            .mappings()
            .one()
        )
        original_steps = (
            await session.execute(
                select(
                    AutomationStepRunModel.id,
                    AutomationStepRunModel.status,
                    AutomationStepRunModel.error_category,
                    AutomationStepRunModel.output_summary_json,
                ).where(AutomationStepRunModel.run_id == run.id)
            )
        ).all()
    assert original_cursor["phase"] == expected_phase
    assert [step.status for step in original_steps] == (
        [] if expected_step_status is None else [expected_step_status]
    )
    if outcome == "accepted":
        assert json.loads(original_cursor["payload_json"])["outputs"]["send"] == {
            "accepted": True,
            "original_receipt": "fixture-receipt",
        }
        assert json.loads(original_steps[0].output_summary_json) == {
            "keys": ["accepted", "original_receipt"]
        }
    if outcome == "not_dispatched":
        assert original_steps[0].error_category == "mcp_request_not_dispatched"

    assert registry.unregister(_RETIRED_CALL)
    clock.advance(31)
    (recovered,) = await repository.claim_due(
        worker_id="after-retirement", now=clock.now(), lease_seconds=30
    )
    worker = AutomationWorker(
        settings=settings, repository=repository, time_service=time_service, executor=executor
    )
    await worker._process(recovered)
    (recorded,) = await repository.run_history(row.id)
    assert recorded.id == run.id
    assert recorded.status is RunStatus.BLOCKED
    assert recorded.error_category == "delegated_authority_revoked"
    assert recorded.result_summary["previous_evidence"] == "retained"
    assert recorded.tool_calls == 1
    assert recorded.steps_completed == int(outcome == "accepted")
    assert recorded.llm_calls == recorded.messages_sent == 0
    assert calls == [(row.id, run.id, "send")]
    assert (await repository.get(row.id)).status is AutomationStatus.BLOCKED
    async with database.sessions() as session:
        assert (
            await session.execute(
                select(budgets.c.models, budgets.c.tools).where(budgets.c.run_id == run.id)
            )
        ).one() == (2, 4)
        assert (
            await session.execute(select(invocations).where(invocations.c.run_id == run.id))
        ).mappings().one() == original_cursor
        assert (
            await session.execute(
                select(
                    AutomationStepRunModel.id,
                    AutomationStepRunModel.status,
                    AutomationStepRunModel.error_category,
                    AutomationStepRunModel.output_summary_json,
                ).where(AutomationStepRunModel.run_id == run.id)
            )
        ).all() == original_steps
    # A later poll cannot create another execution or dispatch the retired call.
    assert not await repository.claim_due(worker_id="later", now=clock.now(), lease_seconds=30)
    assert len(await repository.run_history(row.id)) == 1
