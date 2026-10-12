"""Unique due occurrences activate common Main; original Work owns continuation."""

import asyncio
import hashlib
import json
import time
from contextlib import asynccontextmanager
from datetime import timedelta

import pytest
from sqlalchemy import event, insert, select, update
from tests.support.automation_runtime_helpers import FakeClock
from tests.support.automation_unified_delivery_helpers import sent, setup_run
from tests.support.runtime_execution import make_work_resumer
from tests.support.social_identity_cases import Bot

from qq_ai_bot.automation.gateway import OneBotAutomationGateway
from qq_ai_bot.automation.models import AutomationStatus, RunStatus
from qq_ai_bot.conversation.canonical_db_models import PersonActiveRouteModel, SpaceActiveRouteModel
from qq_ai_bot.conversation.hydrate import bump_canonical_generation
from qq_ai_bot.conversation.scope import ConversationTurnSnapshot
from qq_ai_bot.domain.conversations import ConversationScope
from qq_ai_bot.domain.messages import ChatResponse, ToolCall, ToolFunction
from qq_ai_bot.identity.canonical_repository import ensure_presence, ensure_space
from qq_ai_bot.identity.db_models import SpaceBindingModel
from qq_ai_bot.persistence.models import AutomationModel, AutomationRunModel, AutomationVersionModel
from qq_ai_bot.runtime.origin import TurnOrigin
from qq_ai_bot.runtime.work_activation import bind_work_activation
from qq_ai_bot.runtime.work_budget_schema import automation_budgets, budgets
from qq_ai_bot.runtime.work_control import WorkControl
from qq_ai_bot.runtime.work_recovery_schema import invocations
from qq_ai_bot.runtime.work_scheduler import WorkScheduler
from qq_ai_bot.runtime.work_schema_v1 import effects, journal, work
from qq_ai_bot.runtime.work_wait import WorkWaitRepository
from qq_ai_bot.services.agent_tools import ToolRuntime
from qq_ai_bot.services.execution_sources import recover_automation_source


def calls(*items):
    return ChatResponse(
        "",
        0,
        tool_calls=tuple(
            ToolCall(identity, ToolFunction("task_control", json.dumps(arguments)))
            for identity, arguments in items
        ),
    )


def resumer(case):
    return make_work_resumer(
        case.works,
        ledger=case.chat._ledger,
        scopes=case.chat._conversation_scopes,
        turns=case.chat._turn_coordinator,
        router=case.env.router,
        config=case.chat._runtime_config,
        generate_self=case.chat.generate_self_initiative,
        generate_wakeup=case.chat.generate_main_agent_wakeup,
        bindings=case.chat.runtime.bindings,
        settings=case.settings,
        generate_origin=case.chat.generate_origin_turn,
    )


@pytest.mark.parametrize(
    ("principal", "delivery"),
    [("person", "none"), ("self", "none"), ("person", "self_private")],
)
async def test_paused_send_route_does_not_block_internal_main_read(
    database, tmp_path, principal, delivery
):
    case = await setup_run(
        database,
        tmp_path,
        principal=principal,
        delivery=delivery,
        mode="silent",
        goal="读取当前时间并内部结束，不发送消息",
    )
    responses = iter(
        (
            ChatResponse(
                "", 0, tool_calls=(ToolCall("read-time", ToolFunction("time_get_current", "{}")),)
            ),
            "完成本次内部读取",
        )
    )
    case.provider._responder = lambda _: next(responses)
    async with database.immediate_session() as writer:
        route = await writer.get(
            PersonActiveRouteModel if delivery == "self_private" else SpaceActiveRouteModel,
            case.env.person if delivery == "self_private" else case.env.space,
        )
        route.paused = True
        if delivery != "self_private":
            # A later sending route is not this task's execution target/presence.
            alternate = await ensure_space(writer, "20002")
            alternate_presence = await ensure_presence(writer, "80002")
            binding = await writer.scalar(
                select(SpaceBindingModel).where(SpaceBindingModel.space_id == alternate)
            )
            binding.space_id = case.env.space
            await writer.flush()
            route.space_binding_id = binding.id
            route.presence_id = alternate_presence
    if delivery != "self_private":
        case.env.registry.connect(
            Bot("80002"), provider_id="snowluma", presence_id=alternate_presence
        )
    sources = []
    generate = case.worker._generate

    async def capture(**kwargs):
        sources.append(kwargs["source_runtime"].require_actor())
        return await generate(**kwargs)

    case.worker._generate = capture
    case.clock.advance(2)
    run = await case.dispatch()
    assert run.status is RunStatus.SUCCEEDED and run.llm_calls == 2 and run.tool_calls == 1
    assert sources[0].presence_id == case.row.canonical_presence_id
    assert sources[0].bot_user_id == case.row.bot_user_id
    assert sources[0].principal_kind == principal
    assert sources[0].group_id == (None if delivery == "self_private" else "20001")
    assert not sent(case.env)
    results = [message for message in case.provider.requests[1].messages if message.role == "tool"]
    assert len(results) == 1 and json.loads(results[0].content)["ok"]
    async with database.sessions() as reader:
        assert not list(await reader.execute(select(work)))


async def test_self_next_occurrence_uses_current_generation_without_reopening_old_work(
    database, tmp_path
):
    case = await setup_run(
        database,
        tmp_path,
        principal="self",
        delivery="current_group",
        mode="silent",
        trigger={"type": "interval", "seconds": 60},
    )
    case.provider._responder = lambda _: ChatResponse(
        "",
        0,
        tool_calls=(
            ToolCall(
                "accept-original",
                ToolFunction("task_control", '{"action":"accept","goal":"原工作"}'),
            ),
            ToolCall("send-original", ToolFunction("send_message", '{"text":"原先已发送"}')),
            ToolCall(
                "wait-original",
                ToolFunction(
                    "task_control",
                    '{"action":"wait","conditions":[{"kind":"time_due","after_seconds":0}]}',
                ),
            ),
        ),
    )
    sources = []
    generate = case.worker._generate

    async def capture(**kwargs):
        sources.append(dict(kwargs["source_runtime"].sandbox_source))
        return await generate(**kwargs)

    case.worker._generate = capture
    case.clock.advance(61)
    first = await case.dispatch()
    assert first.status is RunStatus.RUNNING
    async with database.sessions() as reader:
        original = dict((await reader.execute(select(work))).mappings().one())
        original_effects = list((await reader.execute(select(effects))).mappings())
        original_budget = dict((await reader.execute(select(budgets))).mappings().one())
    assert original["state"] == "waiting_external" and original["generation"] == 1
    async with database.immediate_session() as writer:
        assert (
            await bump_canonical_generation(writer, case.env.context.conversation_id, event_id=1)
            == 2
        )
    with pytest.raises(ValueError, match="automation_task_source_changed"):
        await recover_automation_source(
            database,
            original["conversation_id"],
            json.loads(original["source_json"]),
            request_id=original["id"],
            settings=case.settings,
        )
    await case.repository.settle_work_occurrences()
    assert (await case.repository.run_history(case.row.id))[0].status is RunStatus.CANCELLED
    responses = iter(
        (
            ChatResponse(
                "",
                0,
                tool_calls=(
                    ToolCall("read-current", ToolFunction("time_get_current", "{}")),
                    ToolCall(
                        "send-current", ToolFunction("send_message", '{"text":"当前代际的新发生"}')
                    ),
                ),
            ),
            "当前会话的新发生已结束",
        )
    )
    case.provider._responder = lambda _: next(responses)
    case.clock.advance(61)
    second = await case.dispatch()
    assert second.status is RunStatus.SUCCEEDED and second.id != first.id
    assert second.version_id == first.version_id and second.llm_calls == 2
    assert second.messages_sent == 1 and second.tool_calls == 2
    assert sources[0]["generation"] == 1 and sources[1]["generation"] == 2
    assert sources[0]["automation_run_id"] != sources[1]["automation_run_id"]
    assert sources[0]["presence_id"] == sources[1]["presence_id"] == case.row.canonical_presence_id
    current = await case.works.get(original["id"])
    assert current["state"] == "cancelled" and current["generation"] == 1
    assert current["source_json"] == original["source_json"]
    assert current["model_requests"] == original["model_requests"]
    async with database.sessions() as reader:
        assert list((await reader.execute(select(effects))).mappings()) == original_effects
        assert dict((await reader.execute(select(budgets))).mappings().one()) == original_budget
    assert len(sent(case.env)) == 2 and len(case.provider.requests) == 3
    assert sent(case.env)[-1]["message"] == [{"type": "text", "data": {"text": "当前代际的新发生"}}]


@pytest.mark.parametrize("saved_owner", [True, False], ids=["original-work", "missing-owner"])
async def test_original_run_settles_only_its_saved_pending_work(database, tmp_path, saved_owner):
    case = await setup_run(database, tmp_path, delivery="none", mode="silent")
    raw = {
        "version": 1,
        "name": "original two-agent steps",
        "timezone": "UTC",
        "schedule": {"type": "after", "seconds": 1},
        "context": {"scene": "current_group"},
        "steps": [
            {
                "id": identity,
                "call": "yuki.agent",
                "arguments": {"instruction": goal, "delivery_target": "none"},
            }
            for identity, goal in (("sent", "发送第一份原结果"), ("pending", "等待第二份原结果"))
        ],
        "limits": {"max_steps": 2, "max_llm_calls": 10, "timeout_seconds": 600},
    }
    raw_json = json.dumps(raw, ensure_ascii=False)
    saved_hash = hashlib.sha256(
        json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    async with database.immediate_session() as writer:
        for table, identity in (
            (AutomationModel, case.row.id),
            (AutomationVersionModel, case.row.version_id),
        ):
            await writer.execute(
                update(table)
                .where(table.id == identity)
                .values(script_json=raw_json, script_hash=saved_hash)
            )
    case.clock.advance(2)
    claimed = (
        await case.repository.claim_due(
            worker_id="original-claim",
            now=case.clock.now(),
            lease_seconds=30,
            clock=case.clock.now,
        )
    )[0]
    run = await case.repository.create_run(
        claimed.id,
        scheduled_for=claimed.next_run_at,
        actual_started_at=case.clock.now(),
        worker_id=claimed.claimed_by,
        version_id=claimed.version_id,
    )
    identity = ConversationScope.group("80001", "20001")
    state = await case.chat._conversation_scopes.get(identity)
    for step in raw["steps"]:
        execution_id = f"automation:{run.id}:{step['id']}:{saved_hash}"
        source = {
            "owner": "automation",
            "origin": "scheduled_automation",
            "automation_id": claimed.id,
            "automation_run_id": run.id,
            "step_id": step["id"],
            "script_hash": saved_hash,
            "parent_execution_id": execution_id,
            "conversation_id": state.id,
            "generation": state.generation,
            "principal_kind": "person",
            "actor_person_id": case.env.person,
            "actor_user_id": "10001",
            "presence_id": case.env.presence,
            "space_id": case.env.space,
            "bot_user_id": "80001",
            "current_group_id": "20001",
            "instruction": step["arguments"]["instruction"],
        }
        recovered = await recover_automation_source(
            database, state.id, source, request_id=execution_id, settings=case.settings
        )

        async def validate(source=source, execution_id=execution_id):
            await recover_automation_source(
                database, state.id, source, request_id=execution_id, settings=case.settings
            )

        operations = [("task_control", {"action": "accept", "goal": source["instruction"]})]
        if step["id"] == "sent":
            operations.extend(
                (
                    ("send_message", {"text": "ORIGINAL_CONFIRMED_FIRST_RESULT"}),
                    ("task_control", {"action": "complete", "result": "first finished"}),
                )
            )
        else:
            operations.append(
                (
                    "task_control",
                    {"action": "wait", "conditions": [{"kind": "time_due", "after_seconds": 0}]},
                )
            )
        response = ChatResponse(
            "",
            0,
            tool_calls=tuple(
                ToolCall(f"{step['id']}-{index}", ToolFunction(name, json.dumps(arguments)))
                for index, (name, arguments) in enumerate(operations)
            ),
        )
        case.provider._responder = lambda _, response=response: response
        lease = await case.works.acquire(state.id, state.generation)
        boundary = hashlib.sha256(
            json.dumps(
                ["scheduled_automation", state.id, execution_id, []],
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        control = WorkControl(case.works, lease, f"invocation:{boundary}", source, validate)
        async with case.chat._turn_coordinator.background_turn(
            state.runtime_scope_key, origin=TurnOrigin.SCHEDULED_AUTOMATION
        ) as token:
            async with bind_work_activation(control):
                await case.chat.generate_origin_turn(
                    content=source["instruction"],
                    identity=identity,
                    runtime=await case.chat._runtime_config.snapshot(
                        user_id="10001", group_id="20001"
                    ),
                    turn_token=token,
                    turn_snapshot=ConversationTurnSnapshot(
                        state.id,
                        state.runtime_scope_key,
                        state.generation,
                        None,
                        token.version,
                        identity.key,
                        automation_run_id=run.id,
                    ),
                    before_model_request=validate,
                    source_runtime=ToolRuntime(
                        inbound=None,
                        actor_context=recovered.actor(execution_id),
                        allow_generic_onebot=False,
                        gateway=OneBotAutomationGateway(
                            bot_user_id="80001",
                            automation_id=claimed.id,
                            automation_run_id=run.id,
                            router=case.env.router,
                        ),
                        conversation_key=state.runtime_scope_key,
                        execution_id=execution_id,
                        origin=TurnOrigin.SCHEDULED_AUTOMATION,
                        conversation_id=state.id,
                        person_id=case.env.person,
                        space_id=case.env.space,
                        scope_type=identity.scope_type,
                        external_target_id="20001",
                        sandbox_source=source,
                    ),
                )
    async with database.sessions() as reader:
        rows = [dict(row) for row in (await reader.execute(select(work))).mappings()]
    assert sorted(row["state"] for row in rows) == ["completed", "waiting_external"]
    pending = next(row for row in rows if row["state"] == "waiting_external")
    completed = next(row for row in rows if row["state"] == "completed")
    payload = {
        "outputs": {"sent": {"work_id": completed["id"], "state": "completed"}},
        "steps_completed": 1,
        "llm_calls": 2,
        "tool_calls": 5,
        "messages_sent": 1,
        "next_step": 1,
        "active_seconds": 0,
    }
    if saved_owner:
        payload["work_id"] = pending["id"]
    # The old caller saved the accepted pending Work reference, not just a Run id.
    async with database.immediate_session() as writer:
        await writer.execute(
            insert(invocations).values(
                run_id=run.id,
                script_hash=saved_hash,
                phase="agent",
                updated=time.time(),
                payload_json=json.dumps(payload),
            )
        )
        await writer.execute(
            update(AutomationRunModel)
            .where(AutomationRunModel.id == run.id)
            .values(steps_completed=1, llm_calls=2, tool_calls=5, messages_sent=1)
        )

    async def receipts():
        async with database.sessions() as reader:
            return [
                list((await reader.execute(select(table))).mappings())
                for table in (work, budgets, effects, journal, invocations)
            ]

    original_receipts = await receipts()
    await case.repository.settle_work_occurrences()
    waiting_run = (await case.repository.run_history(claimed.id))[0]
    assert waiting_run.status is RunStatus.RUNNING
    assert await receipts() == original_receipts
    assert len(sent(case.env)) == 1 and len(case.provider.requests) == 2

    case.provider._responder = lambda _: calls(
        ("complete-original", {"action": "complete", "result": "second finished"})
    )
    waits = WorkWaitRepository(case.works)
    binding = await waits.describe(pending["id"])
    assert await waits.deliver_due(now=binding["registered_at"] + 1) == 1
    scheduler = WorkScheduler(case.works, resumer(case).resume, chat_admission_enabled=True)
    scheduler._last_reclaim = time.monotonic()
    await scheduler.drain_once()
    current = await case.works.get(pending["id"])
    assert current["state"] == "completed" and current["model_requests"] == 2
    assert current["source_json"] == pending["source_json"]
    assert await case.works.get(completed["id"]) == completed
    settled = (await case.repository.run_history(claimed.id))[0]
    if saved_owner:
        assert settled.status is RunStatus.SUCCEEDED
        assert settled.result_summary == {"work_id": pending["id"], "work_state": "completed"}
        assert (settled.steps_completed, settled.llm_calls, settled.messages_sent) == (1, 2, 1)
        assert (await case.repository.get(claimed.id)).run_count == 1
    else:
        assert settled.status is RunStatus.RUNNING
        assert (await case.repository.get(claimed.id)).run_count == 0
    final_receipts = await receipts()
    await case.repository.settle_work_occurrences()
    await case.repository.settle_work_occurrences()
    assert await receipts() == final_receipts
    assert (await case.repository.run_history(claimed.id))[0] == settled
    assert len(sent(case.env)) == 1 and len(case.provider.requests) == 3


async def test_waiting_occurrence_keeps_original_version_and_current_work_recovery(
    database, tmp_path
):
    case = await setup_run(
        database,
        tmp_path,
        delivery="none",
        mode="silent",
        trigger={"type": "interval", "seconds": 60},
        max_runs=2,
        goal="原始完整目标",
    )
    case.provider._responder = lambda _: calls(
        ("accept", {"action": "accept", "goal": "原始完整任务"}),
        ("wait", {"action": "wait", "conditions": [{"kind": "time_due", "after_seconds": 0}]}),
    )
    case.clock.advance(61)
    first = await case.dispatch()
    assert first.status is RunStatus.RUNNING
    async with database.sessions() as reader:
        original = dict((await reader.execute(select(work))).mappings().one())
        assert not list(await reader.execute(select(automation_budgets)))
    source = json.loads(original["source_json"])
    assert source["automation_run_id"] == first.id
    assert "step_id" not in source and source["script_hash"] == case.row.script_hash
    assert original["state"] == "waiting_external"
    assert (
        await case.repository.claim_due(
            worker_id="scan-again", now=case.clock.now(), lease_seconds=60
        )
        == ()
    )
    changed = await case.service.update_task(
        case.row.id,
        {**case.row.script, "goal": "未来新目标"},
        actor=case.actor,
        conversation_key=case.env.context.conversation_id,
    )
    assert changed.version_id != first.version_id and changed.next_run_at == first.scheduled_for
    original_version = await case.repository.get_for_run(first.id)
    assert original_version.script["goal"] == "原始完整目标"
    assert original_version.script_hash == case.row.script_hash
    # A future schedule switch is not a gate on already accepted Work.
    case.settings.automation_enabled = False
    case.provider._responder = lambda _: calls(
        ("complete", {"action": "complete", "result": "完成原目标"})
    )
    waits = WorkWaitRepository(case.works)
    binding = await waits.describe(original["id"])
    assert await waits.deliver_due(now=binding["registered_at"] + 1) == 1
    scheduler = WorkScheduler(case.works, resumer(case).resume, chat_admission_enabled=True)
    scheduler._last_reclaim = time.monotonic()
    await scheduler.drain_once()
    current_work = await case.works.get(original["id"])
    assert current_work["state"] == "completed"
    assert current_work["source_json"] == original["source_json"]
    completed = (await case.repository.run_history(case.row.id))[0]
    assert completed.id == first.id and completed.version_id == first.version_id
    assert completed.status is RunStatus.SUCCEEDED
    assert completed.llm_calls == current_work["model_requests"] == 2
    current = await case.repository.get(case.row.id)
    assert current.run_count == 1 and current.next_run_at > first.scheduled_for
    case.clock.advance(61)
    case.provider._responder = lambda _: "未来新目标完成"
    case.settings.automation_enabled = True
    next_run = await case.dispatch()
    assert next_run.id != first.id and next_run.version_id == changed.version_id
    async with database.sessions() as reader:
        keys = list(await reader.scalars(select(AutomationRunModel.idempotency_key)))
    assert len(keys) == len(set(keys)) == 2
    assert (await case.repository.get(case.row.id)).run_count == 2
    assert not sent(case.env)


@pytest.mark.parametrize("action", ["pause", "cancel"])
async def test_schedule_management_preserves_already_accepted_work(database, tmp_path, action):
    case = await setup_run(database, tmp_path, delivery="none", mode="silent")
    case.provider._responder = lambda _: calls(
        ("accept", {"action": "accept", "goal": "原始完整任务"}),
        ("wait", {"action": "wait", "conditions": [{"kind": "time_due", "after_seconds": 0}]}),
    )
    case.clock.advance(2)
    first = await case.dispatch()
    async with database.sessions() as reader:
        original = dict((await reader.execute(select(work))).mappings().one())
    assert await getattr(case.service, action)(
        case.row.id, actor=case.actor, conversation_key=case.env.context.conversation_id
    )
    assert await case.works.get(original["id"]) == original
    case.provider._responder = lambda _: calls(
        ("complete", {"action": "complete", "result": "原工作结束"})
    )
    waits = WorkWaitRepository(case.works)
    binding = await waits.describe(original["id"])
    await waits.deliver_due(now=binding["registered_at"] + 1)
    scheduler = WorkScheduler(case.works, resumer(case).resume, chat_admission_enabled=True)
    await scheduler.drain_once()
    assert (await case.works.get(original["id"]))["state"] == "completed"
    run = (await case.repository.run_history(case.row.id))[0]
    assert run.id == first.id and run.status is RunStatus.SUCCEEDED
    assert (await case.repository.get(case.row.id)).status.value == (
        "paused" if action == "pause" else "cancelled"
    )
    assert not sent(case.env)


@pytest.mark.parametrize("status", [RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.UNCERTAIN])
async def test_terminal_occurrence_consumes_explicit_max_runs_once(database, tmp_path, status):
    case = await setup_run(
        database, tmp_path, mode="silent", trigger={"type": "interval", "seconds": 60}, max_runs=1
    )
    original = await case.repository.create_run(
        case.row.id, scheduled_for=case.row.next_run_at, actual_started_at=case.clock.now()
    )
    summary = {
        "result": "x" * 20000,
        "token": "redacted",
        "nested": [{"item": i} for i in range(60)],
    }
    payload = dict(
        status=status,
        steps_completed=0,
        llm_calls=2,
        tool_calls=3,
        messages_sent=0,
        error_category=None if status is RunStatus.SUCCEEDED else "original_failure",
        summary=summary,
        finished_at=case.clock.now(),
    )
    assert await case.repository.finish_run(original.id, **payload)
    assert not await case.repository.finish_run(original.id, **payload)
    current = await case.repository.get(case.row.id)
    assert (
        current.run_count == 1
        and current.next_run_at is None
        and current.status.value == "completed"
    )
    stored = (await case.repository.run_history(case.row.id))[0]
    assert stored.status is status and stored.llm_calls == 2 and stored.tool_calls == 3
    assert stored.result_summary == {**summary, "token": "[redacted]"}
    assert (
        await case.repository.create_run(
            case.row.id,
            scheduled_for=case.clock.now() + timedelta(seconds=60),
            actual_started_at=case.clock.now(),
        )
        is None
    )
    assert not case.provider.requests and not sent(case.env)


async def test_cancelled_occurrence_does_not_cancel_next_recurring_arrangement(database, tmp_path):
    case = await setup_run(
        database, tmp_path, mode="silent", trigger={"type": "interval", "seconds": 60}
    )
    first = await case.repository.create_run(
        case.row.id, scheduled_for=case.row.next_run_at, actual_started_at=case.clock.now()
    )
    await case.repository.finish_run(
        first.id,
        status=RunStatus.CANCELLED,
        steps_completed=0,
        llm_calls=1,
        tool_calls=1,
        messages_sent=0,
        error_category="cancelled",
        summary={},
        finished_at=case.row.next_run_at,
    )
    current = await case.repository.get(case.row.id)
    assert current.status is AutomationStatus.ACTIVE and current.run_count == 1
    assert current.next_run_at > first.scheduled_for


async def test_claimed_old_version_cannot_admit_after_update_or_schedule_cancel(database, tmp_path):
    case = await setup_run(database, tmp_path, mode="silent")
    case.clock.advance(2)
    claimed = (
        await case.repository.claim_due(
            worker_id="old-owner", now=case.clock.now(), lease_seconds=60
        )
    )[0]
    await case.service.update_task(
        case.row.id,
        {**case.row.script, "goal": "新目标"},
        actor=case.actor,
        conversation_key=case.env.context.conversation_id,
    )
    assert (
        await case.repository.create_run(
            case.row.id,
            scheduled_for=claimed.next_run_at,
            actual_started_at=case.clock.now(),
            worker_id="old-owner",
            version_id=claimed.version_id,
        )
        is None
    )
    assert await case.service.cancel(
        case.row.id, actor=case.actor, conversation_key=case.env.context.conversation_id
    )
    assert (
        await case.repository.create_run(
            case.row.id,
            scheduled_for=claimed.next_run_at,
            actual_started_at=case.clock.now(),
            worker_id="old-owner",
            version_id=claimed.version_id,
        )
        is None
    )
    assert await case.repository.run_history(case.row.id) == ()
    assert not case.provider.requests


async def test_unowned_interrupted_occurrence_records_unknown_once_without_model_replay(
    database, tmp_path
):
    case = await setup_run(database, tmp_path, mode="silent")
    first = await case.repository.create_run(
        case.row.id, scheduled_for=case.row.next_run_at, actual_started_at=case.clock.now()
    )
    await case.repository.settle_interrupted_occurrences()
    await case.repository.settle_interrupted_occurrences()
    preserved = (await case.repository.run_history(case.row.id))[0]
    assert preserved.id == first.id and preserved.version_id == first.version_id
    assert preserved.status is RunStatus.UNCERTAIN
    assert (await case.repository.get(case.row.id)).run_count == 1
    case.clock.advance(2)
    assert await case.dispatch() == preserved
    assert not case.provider.requests and not sent(case.env)


@pytest.mark.asyncio
async def test_claim_lease_starts_after_competing_writer_releases(database, tmp_path):
    case = await setup_run(database, tmp_path)
    clock = FakeClock(case.row.next_run_at)
    waiting = asyncio.Event()

    def attempted_writer(_connection, _cursor, statement, _parameters, _context, _many):
        if statement == "BEGIN IMMEDIATE":
            waiting.set()

    async with database.immediate_session():
        event.listen(database.engine.sync_engine, "before_cursor_execute", attempted_writer)
        claim = asyncio.create_task(
            case.repository.claim_due(
                worker_id="real-writer", now=clock.now(), lease_seconds=60, clock=clock.now
            )
        )
        await asyncio.wait_for(waiting.wait(), timeout=5)
        clock.advance(120)
    try:
        claimed = await claim
    finally:
        event.remove(database.engine.sync_engine, "before_cursor_execute", attempted_writer)
    assert [item.id for item in claimed] == [case.row.id]
    expiry = await case.repository.claim_expiry(case.row.id, "real-writer")
    assert expiry == (clock.now() + timedelta(seconds=60)).timestamp()
    assert await case.repository.run_history(case.row.id) == ()
    assert not case.provider.requests and not sent(case.env)


async def test_ordinary_directory_does_not_parse_execution_payloads(database, tmp_path):
    from json import JSONDecodeError

    from sqlalchemy import update
    from tests.support.automation_unified_delivery_helpers import setup_run

    from qq_ai_bot.automation.models import AutomationStatus
    from qq_ai_bot.persistence.models import AutomationModel

    case = await setup_run(database, tmp_path, delivery="none", mode="silent")
    async with database.immediate_session() as writer:
        await writer.execute(
            update(AutomationModel)
            .where(AutomationModel.id == case.row.id)
            .values(
                script_json="unreadable execution IR",
                required_capabilities_json="unreadable capabilities",
                authority_snapshot_json="unreadable authority",
            )
        )
    entry = await case.repository.get_directory_entry(case.row.id)
    assert entry.record.name == case.row.name and entry.creator.person_id == case.env.person
    directory = await case.repository.list_directory(statuses=(AutomationStatus.ACTIVE,))
    assert [item.record.id for item in directory] == [case.row.id]
    with pytest.raises(JSONDecodeError):
        await case.repository.list_directory(
            statuses=(AutomationStatus.ACTIVE,), include_script=True
        )


async def test_batch_claimers_accept_disjoint_sets_and_keep_running_and_versionless_rows(
    database, tmp_path, monkeypatch
):
    case = await setup_run(database, tmp_path, delivery="none", mode="silent")
    clock = FakeClock(case.row.next_run_at)
    async with database.sessions() as reader:
        definition = await reader.get(AutomationModel, case.row.id)
        version = await reader.get(AutomationVersionModel, case.row.version_id)
        definition_values = {
            column.name: getattr(definition, column.name)
            for column in AutomationModel.__table__.columns
            if column.name != "id"
        }
        version_values = {
            column.name: getattr(version, column.name)
            for column in AutomationVersionModel.__table__.columns
            if column.name != "id"
        }
    async with database.immediate_session() as writer:
        await writer.execute(
            update(AutomationModel)
            .where(AutomationModel.id == case.row.id)
            .values(status="paused", next_run_at=None)
        )
        for identity in (2, 3, 4):
            await writer.execute(
                insert(AutomationModel).values(
                    id=identity, **{**definition_values, "next_run_at": clock.now()}
                )
            )
            if identity != 4:
                await writer.execute(
                    insert(AutomationVersionModel).values(
                        **{**version_values, "automation_id": identity}
                    )
                )
    original_writer = database.immediate_session
    both_discovered = asyncio.Event()
    arriving = 0

    @asynccontextmanager
    async def compete():
        nonlocal arriving
        arriving += 1
        if arriving == 2:
            both_discovered.set()
        await asyncio.wait_for(both_discovered.wait(), timeout=5)
        async with original_writer() as writer:
            yield writer

    monkeypatch.setattr(database, "immediate_session", compete)
    first, second = await asyncio.gather(
        *(
            case.repository.claim_due(
                worker_id=worker, now=clock.now(), lease_seconds=60, clock=clock.now
            )
            for worker in ("first-real-writer", "second-real-writer")
        )
    )
    first_ids, second_ids = {row.id for row in first}, {row.id for row in second}
    assert not first_ids & second_ids and first_ids | second_ids == {2, 3, 4}
    accepted = {row.id: row for row in (*first, *second)}
    assert accepted[4].version_id is None
    assert all(row.script == case.row.script for row in accepted.values())
    assert await case.repository.run_history(case.row.id) == ()
    assert not case.provider.requests and not sent(case.env)


@pytest.mark.parametrize("unknown_commit", [False, True])
async def test_claim_parses_only_after_successful_commit(
    database, tmp_path, monkeypatch, unknown_commit
):
    import qq_ai_bot.automation.repository as automation_repository

    case = await setup_run(database, tmp_path, delivery="none", mode="silent")
    original_writer = database.immediate_session
    original_record = automation_repository._automation_record
    committed = False
    parsed = []

    @asynccontextmanager
    async def commit_outcome():
        nonlocal committed
        async with original_writer() as writer:
            yield writer
        if unknown_commit:
            raise RuntimeError("unknown_claim_commit")
        committed = True

    def parse_accepted(row, version_id=None):
        assert committed
        parsed.append(row.id)
        return original_record(row, version_id)

    monkeypatch.setattr(database, "immediate_session", commit_outcome)
    monkeypatch.setattr(automation_repository, "_automation_record", parse_accepted)
    if unknown_commit:
        with pytest.raises(RuntimeError, match="unknown_claim_commit"):
            await case.repository.claim_due(
                worker_id="original-claim", now=case.row.next_run_at, lease_seconds=60
            )
        assert parsed == []
    else:
        accepted = await case.repository.claim_due(
            worker_id="original-claim", now=case.row.next_run_at, lease_seconds=60
        )
        assert parsed == [case.row.id] and accepted[0].script == case.row.script
    async with database.sessions() as reader:
        original = await reader.get(AutomationModel, case.row.id)
        assert original.claimed_by == "original-claim"
    assert not case.provider.requests and not sent(case.env)


async def test_equivalent_directory_match_reaches_the_actual_tail(database, tmp_path):
    case = await setup_run(database, tmp_path, mode="silent")
    requested = {
        "name": "完整候选名称" * 40,
        "goal": "only the last plan matches",
        "trigger": {"type": "after", "seconds": 60},
        "delivery": {"target": "current_group"},
    }
    for index in range(101):
        await case.service.administer_create(
            {**requested, "goal": f"another target {index}"},
            owner_id=case.env.person,
            conversation_id=case.env.context.conversation_id,
            creation_source_key=f"directory:{index}",
        )
    target = await case.service.administer_create(
        requested,
        owner_id=case.env.person,
        conversation_id=case.env.context.conversation_id,
        creation_source_key="directory:tail",
    )
    matches = await case.service.find_equivalent_directory_entries(requested, actor=case.actor)
    assert [entry.record.id for entry in matches] == [target.id]
    assert (
        await case.service.find_equivalent_directory_entries(
            requested, actor=case.actor, max_runs=1
        )
        == ()
    )
    assert not case.provider.requests and not sent(case.env)
