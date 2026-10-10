"""Automation sends explicitly; failed Work resumes without reopening its run."""

import json
from dataclasses import replace

import pytest
from sqlalchemy import select
from tests.support.automation_unified_delivery_helpers import sent, setup_run

from qq_ai_bot.automation.models import RunStatus
from qq_ai_bot.domain.messages import ChatResponse, ToolCall, ToolFunction
from qq_ai_bot.runtime.work_schema_v1 import work


@pytest.mark.parametrize("delivery", ["current_group", "self_private"])
@pytest.mark.parametrize("completion_proposed", [False, True])
async def test_person_notification_uses_explicit_target_without_courtesy_recovery(
    database, tmp_path, delivery, completion_proposed
):
    case = await setup_run(database, tmp_path, delivery=delivery, mode="silent")
    calls = 0

    def respond(request):
        nonlocal calls
        calls += 1
        if completion_proposed and calls == 1:
            return ChatResponse(
                "",
                0,
                tool_calls=(
                    ToolCall(
                        "original-completion-proposal",
                        ToolFunction("task_control", '{"action":"complete"}'),
                    ),
                ),
            )
        if calls == (2 if completion_proposed else 1):
            args = {"text": "PUBLIC_EXPLICIT_SEND"}
            if delivery == "self_private":
                args["target"] = {"kind": "person", "subject_ref": "current_speaker"}
            return ChatResponse(
                "",
                0,
                tool_calls=(
                    ToolCall(
                        "explicit-notification", ToolFunction("send_message", json.dumps(args))
                    ),
                ),
            )
        return "INTERNAL_FINAL_MUST_NOT_AUTO_SEND"

    case.provider._responder = respond
    result = await case.executor.execute(case.row, case.run)
    assert result.status is RunStatus.SUCCEEDED, result
    assert calls == (1 if completion_proposed else 2)
    assert len(sent(case.env)) == int(not completion_proposed)
    actions = [action for action, _ in case.env.bot.calls if action.startswith("send_")]
    assert actions == (
        []
        if completion_proposed
        else ["send_private_msg" if delivery == "self_private" else "send_group_msg"]
    )
    if not completion_proposed:
        assert "PUBLIC_EXPLICIT_SEND" in str(sent(case.env))
    assert "INTERNAL_FINAL" not in str(sent(case.env))
    async with database.sessions() as reader:
        sources = list(await reader.scalars(select(work.c.source_json)))
    assert any(
        json.loads(source).get("owner") == "automation"
        and json.loads(source).get("delivery_target") == delivery
        for source in sources
    )


@pytest.mark.parametrize("delivery", ["current_group", "self_private"])
async def test_unsent_person_notification_final_stops_without_courtesy_correction(
    database, tmp_path, delivery
):
    case = await setup_run(database, tmp_path, delivery=delivery, mode="silent")
    result = await case.executor.execute(case.row, case.run)
    assert result.status is RunStatus.SUCCEEDED
    assert len(case.provider.requests) == 1 and not sent(case.env)


async def test_agent_script_uses_original_work_budget_without_outer_flag(database, tmp_path):
    from qq_ai_bot.automation.authority import PermissionLevel
    from qq_ai_bot.automation.models import AutomationScript
    from qq_ai_bot.automation.validator import AutomationValidator, CreationProvenance

    case = await setup_run(database, tmp_path, delivery="none", mode="silent")
    payload = case.row.script.model_dump(mode="json")
    payload["limits"] = {"max_llm_calls": 0, "max_tool_calls": 1, "max_messages": 0}
    step = payload["steps"][0]
    step["arguments"]["max_model_requests"] = 4
    payload["steps"] = [step, {**step, "id": "second", "save_as": None}]
    script = AutomationScript.model_validate(payload)
    validated = AutomationValidator(
        settings=case.executor._settings, registry=case.executor._registry
    ).validate(
        script,
        CreationProvenance(
            creator_user_id="10001",
            bot_user_id="80001",
            message_id="original-source",
            original_text="Complete the work",
            current_group_id="20001",
            mentioned_user_ids=(),
            permission=PermissionLevel.USER,
        ),
        now_utc=case.clock.now(),
    )
    assert len(validated.script.steps) == 2 and validated.script.uses_runtime_budget
    assert not validated.script.limits.agent_budget_managed
    case.executor._enforce_runtime_limits(case.row, llm_calls=4, tool_calls=4, messages_sent=2)


async def test_none_person_automation_nonempty_internal_final_is_silent(
    database, tmp_path, monkeypatch
):
    case = await setup_run(database, tmp_path, delivery="none", mode="silent")
    handlers = case.executor._registry.require("yuki.agent").handler.__self__
    compose = handlers._generation_composition
    caller = []

    async def capture_caller(arguments, context, **kwargs):
        caller.append((arguments, context))
        return await compose(arguments, context, **kwargs)

    monkeypatch.setattr(handlers, "_generation_composition", capture_caller)
    result = await case.executor.execute(case.row, case.run)
    assert result.status is RunStatus.SUCCEEDED
    assert len(case.provider.requests) == 1 and not sent(case.env)
    async with database.sessions() as reader:
        original = dict((await reader.execute(select(work))).mappings().one())
    monkeypatch.setattr(case.chat, "_history_input_budget", lambda *_args, **_kwargs: 0)
    replay = await handlers.agent(*caller[0])
    assert replay.data["text"] == json.loads(original["checkpoint_json"])["sync_result"]
    assert replay.llm_calls == replay.tool_calls == replay.messages_sent == 0
    async with database.sessions() as reader:
        assert dict((await reader.execute(select(work))).mappings().one()) == original
    assert len(case.provider.requests) == 1 and not sent(case.env)


@pytest.mark.parametrize("principal", ["person", "self"])
async def test_failed_agent_releases_owner_and_explicitly_resumes_original_work(
    database, tmp_path, principal
):
    from datetime import timedelta

    from tests.support.runtime_execution import make_work_resumer
    from tests.unit.test_work_owner_recovery import public_action

    from qq_ai_bot.automation.work_cursor import load
    from qq_ai_bot.automation.worker import AutomationWorker
    from qq_ai_bot.llm.base import LLMAuthenticationError
    from qq_ai_bot.persistence.models import AutomationModel, AutomationRunModel
    from qq_ai_bot.runtime.work_repository import WorkRepository
    from qq_ai_bot.runtime.work_scheduler import WorkScheduler

    case = await setup_run(database, tmp_path, delivery="none", mode="silent", principal=principal)
    worker = AutomationWorker(
        settings=case.executor._settings,
        repository=case.repository,
        executor=case.executor,
        time_service=case.chat._time,
    )

    async def claimed_process():
        claimed = await case.repository.claim_due(
            worker_id=worker._worker_id,
            now=case.row.next_run_at + timedelta(seconds=1),
            lease_seconds=60,
            limit=1,
        )
        assert len(claimed) == 1 and claimed[0].id == case.row.id
        await worker._process(claimed[0])

    def unavailable(request):
        raise LLMAuthenticationError("synthetic provider authentication failure")

    case.provider._responder = unavailable
    await claimed_process()
    async with database.sessions() as reader:
        original = dict((await reader.execute(select(work))).mappings().one())
        run = await reader.get(AutomationRunModel, case.run.id)
        automation = await reader.get(AutomationModel, case.row.id)
        assert original["state"] == "failed" and original["reason"] == "LLMAuthenticationError"
        assert run.status == "failed" and run.finished_at is not None
        assert run.llm_calls == original["model_requests"] == len(case.provider.requests) == 1
        assert automation.status == "failed" and automation.run_count == 1
        assert automation.claimed_by is None and automation.claimed_until is None
        original_run = {column.name: getattr(run, column.name) for column in run.__table__.columns}
        original_owner = {
            column.name: getattr(automation, column.name) for column in automation.__table__.columns
        }
    original_cursor = await load(database, case.run.id, case.row.script_hash)
    assert not await case.repository.claim_due(
        worker_id=worker._worker_id,
        now=case.row.next_run_at + timedelta(seconds=2),
        lease_seconds=60,
        limit=1,
    )
    repository = WorkRepository(database)
    resumed_control = await public_action(database, original, "resume")
    assert resumed_control.success and resumed_control.effective_state["status"] == "queued"
    handlers = case.executor._registry.require("yuki.agent").handler.__self__

    async def resume_automation(item, source):
        await handlers.resume_work(database, item, source)

    resumer = make_work_resumer(
        repository,
        ledger=case.chat._ledger,
        scopes=case.chat._conversation_scopes,
        turns=case.chat._turn_coordinator,
        router=case.env.router,
        config=case.chat._runtime_config,
        generate_self=case.chat.generate_self_initiative,
        generate_wakeup=case.chat.generate_main_agent_wakeup,
        validate_snapshot=case.chat.validate_turn_snapshot,
        bindings=case.chat.runtime.bindings,
    )
    resumer.services = replace(
        resumer.services,
        settings=case.executor._settings,
        resume_automation=resume_automation,
    )
    scheduler = WorkScheduler(repository, resumer.resume, chat_admission_enabled=True)
    case.provider._responder = lambda request: "Verified internal result after provider recovery."
    owner_lease = await repository.acquire(original["conversation_id"], original["generation"])
    await scheduler.drain_once()
    assert len(case.provider.requests) == 1
    await repository.release(owner_lease)
    await scheduler.drain_once()
    async with database.sessions() as reader:
        resumed = (await reader.execute(select(work))).mappings().one()
        run = await reader.get(AutomationRunModel, case.run.id)
        automation = await reader.get(AutomationModel, case.row.id)
        assert resumed["id"] == original["id"] and resumed["state"] == "completed"
        assert resumed["source_json"] == original["source_json"]
        assert (
            resumed["model_requests"]
            == original["model_requests"] + 1
            == len(case.provider.requests)
        )
        assert json.loads(resumed["source_json"])["automation_run_id"] == run.id == case.run.id
        assert {
            column.name: getattr(run, column.name) for column in run.__table__.columns
        } == original_run
        assert {
            column.name: getattr(automation, column.name) for column in automation.__table__.columns
        } == original_owner
        assert len(list(await reader.scalars(select(AutomationRunModel.id)))) == 1
    assert await load(database, case.run.id, case.row.script_hash) == original_cursor
    assert not sent(case.env)


@pytest.mark.parametrize("principal", ["person", "self"])
@pytest.mark.parametrize("wait_kind", ["need_input", "time_due"])
async def test_public_cancel_settles_owning_run_without_error_or_new_request(
    database, tmp_path, principal, wait_kind
):
    from datetime import timedelta

    from tests.unit.test_work_owner_recovery import public_action

    from qq_ai_bot.automation.worker import AutomationWorker
    from qq_ai_bot.persistence.models import AutomationModel, AutomationRunModel

    case = await setup_run(database, tmp_path, delivery="none", mode="silent", principal=principal)
    worker = AutomationWorker(
        settings=case.executor._settings,
        repository=case.repository,
        executor=case.executor,
        time_service=case.chat._time,
    )

    async def process():
        claimed = await case.repository.claim_due(
            worker_id=worker._worker_id,
            now=case.row.next_run_at + timedelta(seconds=10),
            lease_seconds=60,
            limit=1,
        )
        assert len(claimed) == 1
        await worker._process(claimed[0])

    case.provider._responder = lambda request: ChatResponse(
        "",
        0,
        tool_calls=(
            ToolCall(
                "wait-original-input",
                ToolFunction(
                    "task_control",
                    json.dumps(
                        {"action": "need_input"}
                        if wait_kind == "need_input"
                        else {
                            "action": "wait",
                            "conditions": [{"kind": "time_due", "after_seconds": 3600}],
                        }
                    ),
                ),
            ),
        ),
    )
    await process()
    async with database.sessions() as reader:
        original = dict((await reader.execute(select(work))).mappings().one())
    assert original["state"] == (
        "waiting_user" if wait_kind == "need_input" else "waiting_external"
    )
    if wait_kind == "time_due":
        # Owner polls retain the original wait without another model/tool.
        await process()
        await process()
        assert len(case.provider.requests) == 1
    cancelled = await public_action(database, original, "cancel")
    assert cancelled.success
    async with database.sessions() as reader:
        released = await reader.get(AutomationModel, case.row.id)
        assert released.claimed_by is None
        if wait_kind == "time_due":
            from qq_ai_bot.runtime.work_wait_schema import waits

            assert await reader.scalar(select(waits.c.status)) == "cancelled"
    await process()
    async with database.sessions() as reader:
        terminal = (await reader.execute(select(work))).mappings().one()
        run = await reader.get(AutomationRunModel, case.run.id)
        automation = await reader.get(AutomationModel, case.row.id)
        assert terminal["id"] == original["id"] and terminal["state"] == "cancelled"
        assert terminal["source_json"] == original["source_json"]
        assert terminal["model_requests"] == original["model_requests"] == 1
        assert run.status == "cancelled" and run.finished_at is not None
        assert run.error_category is None and run.llm_calls == 1
        from qq_ai_bot.persistence.models import AutomationStepRunModel

        step = await reader.scalar(
            select(AutomationStepRunModel).where(AutomationStepRunModel.run_id == run.id)
        )
        assert step.status == "cancelled" and step.error_category is None
        assert automation.status == "cancelled" and automation.run_count == 1
        assert automation.claimed_by is None and automation.claimed_until is None
        assert automation.next_run_at is None
    assert len(case.provider.requests) == 1 and not sent(case.env)
