"""A due occurrence reaches common Main as an execution, with real Social effects."""

import json
import time
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select
from tests.integration.test_automation_lifecycle import calls, resumer
from tests.support.automation_unified_delivery_helpers import sent, setup_run
from tests.support.event_seed import seed_scoped_event

from yuki.automation.models import RunStatus
from yuki.domain.conversations import ConversationScope
from yuki.domain.messages import ChatResponse, ToolCall, ToolFunction
from yuki.domain.tool_actor import ToolActor
from yuki.identity.canonical_repository import (
    create_person_binding,
    create_presence,
    create_space_binding,
)
from yuki.identity.db_models import IdentityBindingModel
from yuki.persistence.models import AutomationModel
from yuki.plugin_host.facades import HostPluginContext, PluginFacadeServices
from yuki.runtime.origin import TurnOrigin
from yuki.runtime.work_scheduler import WorkScheduler
from yuki.runtime.work_schema_v1 import work
from yuki.runtime.work_wait import WorkWaitRepository
from yuki_plugin_sdk.permissions import PluginPermission


def runtime_blocks(request):
    return [
        json.loads(
            message.content.split("<runtime_context>\n", 1)[1].split("\n</runtime_context>", 1)[0]
        )
        for message in request.messages
        if "<runtime_context>\n" in (message.content or "")
    ]


@pytest.mark.parametrize("principal", ["person", "self"])
async def test_due_relative_request_has_original_occurrence_and_frozen_activation_time(
    database, tmp_path, principal
):
    goal = "一个小时后提醒原创建者背单词。现在到期时发送：该背单词了。"
    case = await setup_run(
        database,
        tmp_path,
        principal=principal,
        mode="silent",
        goal=goal,
        trigger={"type": "after", "seconds": 3600},
    )
    created_at, scheduled_for = case.row.created_at, case.row.next_run_at
    case.clock.advance(1800)
    updated = await case.service.update_task(
        case.row.id,
        {**case.row.script, "name": "只修改显示名"},
        actor=case.actor,
        conversation_key=case.actor.conversation_id,
    )
    assert updated.created_at == created_at and updated.next_run_at == scheduled_for
    case.clock.advance(1802)
    activated_at = case.clock.now()

    def respond(request):
        if len(case.provider.requests) == 1:
            case.clock.advance(60)
            return ChatResponse(
                "",
                0,
                tool_calls=(ToolCall("due-send", ToolFunction("send", '{"text":"该背单词了。"}')),),
            )
        return "内部执行完成"

    case.provider._responder = respond
    result = await case.dispatch()
    first, second = case.provider.requests
    context = runtime_blocks(first)[0]
    assert context["origin"] == "scheduled_automation"
    assert (context["automation_id"], context["run_id"]) == (case.row.id, result.id)
    assert context["creator"] == {
        "kind": principal,
        "person_id": case.env.person if principal == "person" else None,
    }
    assert context["source"] == {
        "conversation_id": case.actor.conversation_id,
        "platform": "qq",
    }
    assert datetime.fromisoformat(context["created_at"]) == created_at
    assert datetime.fromisoformat(context["scheduled_for"]) == scheduled_for
    assert datetime.fromisoformat(context["current_time"]) == activated_at
    assert "已经到期" in context["instruction"] and "相对时间属于原安排" in context["instruction"]
    assert "goal" not in context
    assert first.messages[-1].content.endswith(goal)
    assert runtime_blocks(second) == [context]
    assert tuple(second.messages[: len(first.messages)]) == first.messages
    assert result.status is RunStatus.SUCCEEDED and result.messages_sent == 1
    assert len(sent(case.env)) == 1
    async with database.read_session() as reader:
        assert await reader.scalar(select(func.count()).select_from(AutomationModel)) == 1


@pytest.mark.parametrize("principal", ["person", "self"])
async def test_due_internal_final_stays_internal_without_a_completion_send_gate(
    database, tmp_path, principal
):
    case = await setup_run(database, tmp_path, principal=principal, mode="silent")
    case.provider._responder = lambda _: "NO_REPLY"
    case.clock.advance(2)
    result = await case.dispatch()
    assert result.status is RunStatus.SUCCEEDED
    assert result.llm_calls == 1 and result.messages_sent == 0
    assert not sent(case.env)
    if principal == "self":
        assert result.result_summary == {
            "result": "",
            "messages_sent_semantics": "social_successful_parts",
        }


@pytest.mark.parametrize("principal", ["person", "self"])
async def test_plugin_main_uses_original_scheduled_source_and_reuses_confirmed_result(
    database, tmp_path, principal
):
    case = await setup_run(database, tmp_path, principal=principal, mode="silent")
    source = None
    generate = case.worker._generate

    async def capture(**kwargs):
        nonlocal source
        source = kwargs["source_runtime"]
        return await generate(**kwargs)

    case.worker._generate = capture
    case.clock.advance(2)
    occurrence = await case.dispatch()
    host = HostPluginContext(
        plugin_id="test.automation-main",
        approved_permissions=(PluginPermission.AGENT_RUN,),
        services=PluginFacadeServices(
            approval_revision="original-automation-plugin",
            ledger=case.chat._ledger,
            agent_runner=case.chat.runtime.runner,
            runtime_config=case.chat._runtime_config,
        ),
    )
    case.provider._responder = lambda _: calls(
        ("complete-plugin", {"action": "complete", "result": "插件内部工作完成"})
    )
    try:
        with host.invocation_scope(host.plugin_id, source):
            result = await host.agent.run("沿原安排来源处理插件工作")
            repeated = await host.agent.run("沿原安排来源处理插件工作")
        assert result.ok and result.data["state"] == "completed"
        assert repeated.data["work_id"] == result.data["work_id"]
        assert repeated.data["text"] == result.data["text"] == "插件内部工作完成"
        assert repeated.data["model_requests"] == 0
        assert len(case.provider.requests) == 2
        context = runtime_blocks(case.provider.requests[-1])[0]
        assert context["origin"] == "scheduled_automation"
        assert (context["automation_id"], context["run_id"]) == (case.row.id, occurrence.id)
        assert context["creator"] == {
            "kind": principal,
            "person_id": case.env.person if principal == "person" else None,
        }
        assert context["source"] == {
            "conversation_id": case.actor.conversation_id,
            "platform": "qq",
        }
        assert datetime.fromisoformat(context["created_at"]) == case.row.created_at
        assert datetime.fromisoformat(context["scheduled_for"]) == case.row.next_run_at
        assert not sent(case.env)
    finally:
        await host.close_host_resources()


async def test_due_agent_can_create_an_independent_followup(database, tmp_path):
    case = await setup_run(database, tmp_path, mode="silent", goal="安排明天的独立检查，内部结束")

    def respond(request):
        if len(case.provider.requests) == 1:
            return ChatResponse(
                "",
                0,
                tool_calls=(
                    ToolCall(
                        "independent-followup",
                        ToolFunction(
                            "automation_manage",
                            json.dumps(
                                {
                                    "action": "create",
                                    "task": {
                                        "goal": "明天检查新的资料",
                                        "trigger": {"type": "after", "seconds": 86400},
                                    },
                                }
                            ),
                        ),
                    ),
                ),
            )
        return "新的检查已按原回执安排"

    case.provider._responder = respond
    case.clock.advance(2)
    result = await case.dispatch()
    receipt = json.loads(case.provider.requests[1].messages[-1].content)
    assert receipt["ok"] and receipt["data"]["confirmation"] == "persisted"
    assert result.status is RunStatus.SUCCEEDED and not sent(case.env)
    async with database.read_session() as reader:
        assert await reader.scalar(select(func.count()).select_from(AutomationModel)) == 2


async def test_resumed_work_has_one_current_goal_and_keeps_original_occurrence(database, tmp_path):
    case = await setup_run(database, tmp_path, mode="silent", goal="原来安排的完整目标")
    current_goal = "已经核实原资料；继续更新后的工作目标"
    case.provider._responder = lambda _: calls(
        ("accept", {"action": "accept", "goal": "最初接纳的目标"}),
        ("update", {"action": "update", "goal": current_goal}),
        ("wait", {"action": "wait", "conditions": [{"kind": "time_due", "after_seconds": 0}]}),
    )
    case.clock.advance(2)
    original_run = await case.dispatch()
    async with database.read_session() as reader:
        original_work = dict((await reader.execute(select(work))).mappings().one())
    await case.service.update_task(
        case.row.id,
        {**case.row.script, "goal": "下次安排的新要求"},
        actor=case.actor,
        conversation_key=case.actor.conversation_id,
    )
    case.provider._responder = lambda _: calls(
        ("complete", {"action": "complete", "result": "完成"})
    )
    waits = WorkWaitRepository(case.works)
    binding = await waits.describe(original_work["id"])
    assert await waits.deliver_due(now=binding["registered_at"] + 1) == 1
    scheduler = WorkScheduler(case.works, resumer(case).resume, chat_admission_enabled=True)
    scheduler._last_reclaim = time.monotonic()
    await scheduler.drain_once()
    request = case.provider.requests[-1]
    context = runtime_blocks(request)[0]
    assert context["goal"] == current_goal
    assert context["run_id"] == original_run.id and context["automation_id"] == case.row.id
    materials = [
        json.loads(message.content)
        for message in request.messages
        if (message.content or "").lstrip().startswith('{"kind": "work_current_material"')
    ]
    assert len(materials) == 1 and "goal" not in materials[0]
    host_texts = [
        message.content
        for message in request.messages
        if "<runtime_context>" in (message.content or "")
        or (message.content or "").lstrip().startswith('{"kind": "work_current_material"')
        or (message.content or "").startswith("继续原 Work")
    ]
    assert "\n".join(host_texts).count(current_goal) == 1
    assert all("下次安排的新要求" not in text for text in host_texts)
    finished_work = await case.works.get(original_work["id"])
    finished_run = (await case.repository.run_history(case.row.id))[0]
    assert finished_work["id"] == original_work["id"]
    assert finished_work["source_json"] == original_work["source_json"]
    assert finished_run.id == original_run.id and finished_run.version_id == original_run.version_id
    assert finished_work["model_requests"] == finished_run.llm_calls == 2
    assert finished_run.status is RunStatus.SUCCEEDED and not sent(case.env)


async def feishu_source_case(database, tmp_path, *, goal=None):
    case = await setup_run(database, tmp_path, recipient="person", mode="silent")
    await case.service.pause(
        case.row.id, actor=case.actor, conversation_key=case.actor.conversation_id
    )
    now = datetime.now(UTC)
    async with database.immediate_session() as writer:
        author = await create_person_binding(
            writer,
            external_account_id="cli_source:ou_author",
            display_name="飞书原创建者",
            now=now,
            platform="feishu",
        )
        presence = await create_presence(
            writer, external_account_id="cli_source:ou_bot", now=now, platform="feishu"
        )
        source_group = await create_space_binding(
            writer,
            external_space_id="oc_source",
            name="飞书来源群",
            enabled=True,
            autonomous_enabled=False,
            require_mention=False,
            now=now,
            platform="feishu",
        )
    incoming = await seed_scoped_event(
        case.env.service.writer,
        scope=ConversationScope.group(
            presence.external_account_id,
            source_group.external_space_id,
            platform="feishu",
            presence_id=presence.id,
        ),
        origin_key="om_original_request",
        sender_user_id=author.external_account_id,
        direction="inbound",
        content="一个小时后在 QQ 私聊提醒另一个人背单词",
    )
    actor = ToolActor(
        user_id=author.external_account_id,
        bot_user_id=presence.external_account_id,
        group_id=source_group.external_space_id,
        origin=TurnOrigin.USER_MESSAGE,
        instruction=incoming.event.content,
        event_id=incoming.event.id,
        person_id=author.person_id,
        conversation_id=incoming.event.canonical_conversation_id,
        presence_id=presence.id,
        platform="feishu",
        space_id=source_group.space_id,
    )
    async with database.read_session() as reader:
        qq_binding = await reader.scalar(
            select(IdentityBindingModel).where(IdentityBindingModel.person_id == case.env.person)
        )
    target = {"kind": "person", "target_id": case.env.person, "binding_id": qq_binding.id}
    task = await case.service.create_task(
        {
            "goal": goal or f"一个小时后在 QQ 私聊提醒此目标背单词：{json.dumps(target)}",
            "trigger": {"type": "after", "seconds": 3600},
        },
        actor=actor,
        conversation_key=actor.conversation_id,
    )
    return case, task, actor, target


async def test_feishu_source_keeps_its_scene_while_sending_to_another_qq_person(database, tmp_path):
    case, task, actor, target = await feishu_source_case(database, tmp_path)
    case.provider._responder = lambda _: (
        ChatResponse(
            "",
            0,
            tool_calls=(
                ToolCall(
                    "qq-reminder",
                    ToolFunction("send", json.dumps({"text": "该背单词了。", "target": target})),
                ),
            ),
        )
        if len(case.provider.requests) == 1
        else "内部执行结束"
    )
    case.clock.advance(3602)
    async for claimed in case.repository.claim_due(
        worker_id="cross-platform", now=case.clock.now(), lease_seconds=120, clock=case.clock.now
    ):
        await case.worker._process_guarded(claimed)
    result = (await case.repository.run_history(task.id))[0]
    context = runtime_blocks(case.provider.requests[0])[0]
    assert context["creator"]["person_id"] == actor.person_id
    assert context["source"] == {"conversation_id": actor.conversation_id, "platform": "feishu"}
    assert result.status is RunStatus.SUCCEEDED and result.messages_sent == 1
    assert [action for action, _ in case.env.bot.calls if action.startswith("send_")] == [
        "send_private_msg"
    ]
    assert sent(case.env)[0]["user_id"] == 10001
