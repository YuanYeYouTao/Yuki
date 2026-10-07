"""Communication guarantees through the real Runner, journal and effect receipts."""

import json
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select
from tests.conftest import build_harness, make_settings

# P10: explicit Invocation fixture contract; existing assertions are retained.
from tests.support.agent_backend import StubAgentBackend
from tests.support.social_identity_cases import social_env

from qq_ai_bot.automation.models import TurnOrigin
from qq_ai_bot.domain.messages import ChatMessage, ChatResponse, ChatTool, ToolCall, ToolFunction
from qq_ai_bot.llm.fake import FakeLLMProvider
from qq_ai_bot.persistence.models import ChatEventModel
from qq_ai_bot.runtime.subagent_tools import subagent_tools
from qq_ai_bot.runtime.work_control import WorkControl, work_control_tools
from qq_ai_bot.runtime.work_repository import WorkRepository
from qq_ai_bot.runtime.work_schema_v1 import effects
from qq_ai_bot.services.agent_runner import AgentRuntime


async def new_event(test_case, text):
    from qq_ai_bot.conversation.rollup.models import RollupPolicyConfig
    from qq_ai_bot.domain.conversations import ConversationScope
    from qq_ai_bot.persistence.scoped_event_uow import ScopedEventLedgerUnitOfWork

    platform_reference = str(uuid4())
    await ScopedEventLedgerUnitOfWork(
        test_case.repository.database, config=RollupPolicyConfig()
    ).append(
        scope=ConversationScope.group("80001", "20001"),
        platform_message_id=platform_reference,
        sender_user_id="10001",
        direction="inbound",
        content=text,
    )
    async with test_case.repository.database.sessions() as reader:
        return await reader.scalar(
            select(ChatEventModel.id).where(
                ChatEventModel.platform_message_id == platform_reference
            )
        )


def tool(name, args=None, identity=None):
    return ToolCall(identity or name, ToolFunction(name, json.dumps(args or {})))


def response(*calls):
    return ChatResponse("", 0, tool_calls=tuple(calls))


START = {"text": "先查原因，再修复。", "work_report": {"kind": "start"}}


async def case(database, tmp_path, responses, *, reporting="interactive", send_status="succeeded"):
    env = await social_env(database, tmp_path)
    repository = WorkRepository(database)
    lease = await repository.acquire(env.context.conversation_id, 1)
    assert lease

    async def validate():
        assert await repository.valid(lease)

    source = {"actor_person_id": env.person, "principal_kind": "person", "origin": "user_message"}
    control = WorkControl(repository, lease, "report-runner", source, validate)
    control.current = await repository.accept(
        lease,
        source_key="report-runner",
        source=source,
        goal="调查并修复",
        output_kind="state_change",
        reporting=reporting,
    )
    scripted = iter(responses)
    provider = FakeLLMProvider(lambda _: next(scripted))
    harness = build_harness(database, make_settings(database.url), provider)
    chat = harness.processor._chat
    observed = []

    class Backend(StubAgentBackend):
        def definitions(self, runtime, **kwargs):
            return (
                *work_control_tools(),
                *subagent_tools(),
                ChatTool("send_message", "send", {"type": "object"}),
                ChatTool("read_fixture", "read", {"type": "object"}),
                ChatTool("write_fixture", "write", {"type": "object"}),
            )

        def begin_batch(self, *args):
            pass

        def parallel_safe(self, name, runtime):
            return name == "read_fixture"

        def is_side_effecting(self, name, arguments, runtime):
            return name != "read_fixture"

        async def execute_call(self, invocation):
            name = invocation.call.function.name
            arguments = invocation.call.function.arguments
            observed.append(name)
            if name == "send_message":
                target = json.loads(arguments).get("target", {"kind": "space", "id": env.space})
                return json.dumps(
                    {
                        "ok": send_status == "succeeded",
                        "data": {
                            "status": send_status,
                            "target": target,
                        },
                    }
                )
            return json.dumps({"ok": True, "data": {"exit_code": 0}})

        def finalize(self, text, runtime):
            return text

        def exhausted(self, runtime):
            return "exhausted"

        def post_commit_recovery_text(self):
            return None

    runtime = AgentRuntime(
        origin=TurnOrigin.USER_MESSAGE,
        actor_user_id="10001",
        actor_is_superuser=False,
        delegated_authority=None,
        conversation_key="report-runner",
        current_group_id=None,
        bot_user_id="80001",
        gateway=None,
        runtime_config=await chat._runtime_config.snapshot(),
        current_time=chat._time.current_default(),
        allowed_capabilities=frozenset(),
        max_tool_calls=12,
        max_model_requests=12,
        work_control=control,
    )
    return SimpleNamespace(
        env=env,
        repository=repository,
        control=control,
        provider=provider,
        runner=chat.runtime.runner,
        runtime=runtime,
        backend=Backend(),
        observed=observed,
        chat=chat,
    )


async def run(test_case):
    return await test_case.runner.run(
        (ChatMessage("user", "请修复并报告"),), test_case.runtime, test_case.backend
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("order", ["first", "last"])
async def test_start_serializes_reads_without_reordering_or_charging_rejections(
    database, tmp_path, order
):
    reads = (tool("read_fixture", {"key": "a"}, "r1"), tool("read_fixture", {"key": "b"}, "r2"))
    first = (
        (tool("send_message", START), *reads)
        if order == "first"
        else (*reads, tool("send_message", START))
    )
    test_case = await case(
        database,
        tmp_path,
        [
            response(*first),
            *([response(*reads)] if order == "last" else []),
            response(tool("write_fixture")),
            response(tool("task_control", {"action": "complete"})),
        ],
    )
    result = await run(test_case)
    assert result.work_state == "completed"
    assert test_case.observed == ["send_message", "read_fixture", "read_fixture", "write_fixture"]
    assert test_case.control.tools_started == 4
    async with database.sessions() as reader:
        receipt_rows = (
            (await reader.execute(select(effects).where(effects.c.work_id == result.work_id)))
            .mappings()
            .all()
        )
    assert len([row for row in receipt_rows if row["kind"] == "tool"]) == 4
    if order == "last":
        serialized = str(test_case.provider.requests[1].messages)
        assert serialized.count("work_start_required") == 2
    for previous, following in zip(
        test_case.provider.requests, test_case.provider.requests[1:], strict=False
    ):
        assert following.tools == previous.tools
        assert following.messages[: len(previous.messages)] == previous.messages


@pytest.mark.asyncio
@pytest.mark.parametrize("delegate", ["subagent_start", "subagent_message"])
async def test_subagent_start_cannot_bypass_first_business_barrier(database, tmp_path, delegate):
    test_case = await case(
        database,
        tmp_path,
        [
            response(
                tool(
                    delegate,
                    {"goal": "child", "acceptance": "proof", "output_kind": "answer"},
                )
            ),
            response(tool("send_message", START)),
            response(tool("write_fixture")),
            response(tool("task_control", {"action": "complete"})),
        ],
    )
    result = await run(test_case)
    assert result.work_state == "completed"
    assert "work_start_required" in str(test_case.provider.requests[1].messages)
    assert test_case.control.communication["start_feedback_given"] is True
    from qq_ai_bot.runtime.subagent_repository import SubagentRepository

    assert await SubagentRepository(test_case.repository).unfinished(result.work_id) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("send_status", ["failed", "unknown"])
async def test_unconfirmed_start_blocks_business_without_resend_or_tool_charge(
    database, tmp_path, send_status
):
    test_case = await case(
        database,
        tmp_path,
        [
            response(tool("send_message", START), tool("write_fixture")),
            response(tool("task_control", {"action": "fail", "reason": "交付未确认"})),
        ],
        send_status=send_status,
    )
    result = await run(test_case)
    assert result.work_state != "completed"
    assert test_case.observed == ["send_message"]
    assert test_case.control.tools_started == 1
    assert "work_start_delivery_unconfirmed" in str(test_case.provider.requests[1].messages)


@pytest.mark.asyncio
async def test_repeated_start_omission_consumes_one_batch_correction_and_stops(database, tmp_path):
    test_case = await case(
        database,
        tmp_path,
        [
            response(
                tool("read_fixture", {"key": "a"}, "r1"), tool("write_fixture", identity="w1")
            ),
            response(tool("write_fixture", identity="w2")),
        ],
    )
    result = await run(test_case)
    assert result.work_state != "completed"
    assert test_case.observed == []
    assert test_case.control.tools_started == 0
    assert len(test_case.provider.requests) == 2
    assert test_case.control.communication["start_feedback_given"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("second_final", [False, True])
async def test_interactive_internal_final_requires_explicit_exit_once(
    database, tmp_path, second_final
):
    test_case = await case(
        database,
        tmp_path,
        [
            response(tool("send_message", START)),
            response(tool("write_fixture")),
            ChatResponse("只是阶段发现", 0),
            ChatResponse("仍只有内部正文", 0)
            if second_final
            else response(tool("task_control", {"action": "complete"})),
        ],
    )
    result = await run(test_case)
    assert (result.work_state == "completed") is not second_final
    assert len(test_case.provider.requests) == 4
    assert test_case.control.communication["final_feedback_given"] is True
    assert "不能据此结束交互式 Work" in str(test_case.provider.requests[-1].messages)


@pytest.mark.asyncio
@pytest.mark.parametrize("reporting", [None, "quiet"])
async def test_legacy_and_quiet_steer_get_one_nonblocking_reply_opportunity(
    database, tmp_path, reporting
):
    test_case = await case(
        database,
        tmp_path,
        [
            response(tool("write_fixture")),
            response(tool("task_control", {"action": "complete"})),
        ],
        reporting=reporting,
    )
    async with database.sessions() as reader:
        event_id = await reader.scalar(select(ChatEventModel.id))
    identity = await test_case.repository.enqueue(
        test_case.control.lease.conversation_id,
        1,
        "new-steer",
        kind="message",
        event_id=event_id,
        work_id=test_case.control.current["id"],
        ready=False,
    )
    await test_case.repository.prepare_input(identity, {"text": "现在进展如何？"})
    result = await run(test_case)
    assert result.work_state == "completed"
    assert test_case.observed == ["write_fixture"]
    messages = test_case.provider.requests[-1].messages
    assert sum("原 Work 新输入的答复机会" in (message.content or "") for message in messages) == 1
    assert test_case.control.communication["input_feedback_through_id"] > 0


@pytest.mark.asyncio
async def test_rejected_business_does_not_exhaust_budget_before_later_start(database, tmp_path):
    test_case = await case(
        database,
        tmp_path,
        [
            response(tool("write_fixture"), tool("send_message", START)),
        ],
    )
    test_case.runtime = replace(test_case.runtime, max_tool_calls=1, max_model_requests=1)
    await run(test_case)
    assert test_case.observed == ["send_message"]
    assert test_case.control.tools_started == 1


@pytest.mark.asyncio
async def test_stage_and_new_input_share_one_nonblocking_opportunity(database, tmp_path):
    test_case = await case(
        database,
        tmp_path,
        [
            response(tool("send_message", START)),
            ChatResponse("已定位一处来源差异，继续检查。", 0, tool_calls=(tool("read_fixture"),)),
            response(tool("write_fixture")),
            response(tool("task_control", {"action": "complete"})),
        ],
    )
    async with database.sessions() as reader:
        event_id = await reader.scalar(select(ChatEventModel.id))
    original_execute = test_case.backend.execute_call

    async def execute(invocation):
        name = invocation.call.function.name
        result = await original_execute(invocation)
        if name == "read_fixture":
            identity = await test_case.repository.enqueue(
                test_case.control.lease.conversation_id,
                1,
                "stage-steer",
                kind="message",
                event_id=event_id,
                work_id=test_case.control.current["id"],
                ready=False,
            )
            await test_case.repository.prepare_input(identity, {"text": "问题找到了吗？"})
        return result

    test_case.backend.execute_call = execute
    result = await run(test_case)
    assert result.work_state == "completed"
    opportunities = [
        message
        for message in test_case.provider.requests[2].messages
        if "上一段工具结果仍是内部资料" in (message.content or "")
    ]
    assert len(opportunities) == 1
    assert "原 Work 新输入的答复机会" in opportunities[0].content
    assert len(test_case.control.communication["stage_feedback_batch"]) == 64
    assert test_case.observed == ["send_message", "read_fixture", "write_fixture"]


@pytest.mark.asyncio
async def test_invalid_association_does_not_consume_last_allowance_or_send(database, tmp_path):
    test_case = await case(
        database,
        tmp_path,
        [
            response(
                tool(
                    "send_message",
                    {
                        "text": "不可关联其他输入",
                        "work_report": {"kind": "reply", "reply_to_event_ids": [999999]},
                    },
                    "invalid",
                ),
                tool("send_message", START, "valid"),
            ),
        ],
    )
    test_case.runtime = replace(test_case.runtime, max_tool_calls=1, max_model_requests=1)
    await run(test_case)
    assert test_case.observed == ["send_message"]
    assert test_case.control.tools_started == 1
    async with database.sessions() as reader:
        rows = (
            (
                await reader.execute(
                    select(effects).where(
                        effects.c.work_id == test_case.control.current["id"],
                        effects.c.kind == "tool",
                    )
                )
            )
            .mappings()
            .all()
        )
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_explicit_no_reply_with_tools_does_not_trigger_stage_prompt(database, tmp_path):
    test_case = await case(
        database,
        tmp_path,
        [
            response(tool("send_message", START)),
            ChatResponse("NO_REPLY", 0, tool_calls=(tool("write_fixture"),)),
            response(tool("task_control", {"action": "complete"})),
        ],
    )
    result = await run(test_case)
    assert result.work_state == "completed"
    assert not any(
        "上一段工具结果仍是内部资料" in (message.content or "")
        for message in test_case.provider.requests[-1].messages
    )
    assert "stage_feedback_batch" not in test_case.control.communication


@pytest.mark.asyncio
async def test_start_correction_is_not_reset_by_journal_recovery(database, tmp_path):
    test_case = await case(
        database,
        tmp_path,
        [
            response(tool("read_fixture", identity="first")),
            response(tool("write_fixture", identity="second")),
        ],
    )
    test_case.runtime = replace(test_case.runtime, max_model_requests=1)
    first = await run(test_case)
    assert first.work_state == "queued"
    recovered = WorkControl(
        test_case.repository,
        test_case.control.lease,
        test_case.control.source_key,
        test_case.control.source,
        test_case.control.validate,
    )
    recovered.current = await test_case.repository.get(first.work_id)
    test_case.control = recovered
    test_case.runtime = replace(test_case.runtime, work_control=recovered, max_model_requests=4)
    second = await run(test_case)
    assert second.work_state != "completed"
    assert len(test_case.provider.requests) == 2
    assert test_case.observed == []
    assert recovered.communication["start_feedback_given"] is True


@pytest.mark.asyncio
async def test_consumed_input_reply_opportunity_survives_resume_once(database, tmp_path):
    test_case = await case(
        database,
        tmp_path,
        [
            response(tool("write_fixture")),
            response(tool("task_control", {"action": "complete"})),
        ],
        reporting="quiet",
    )
    async with database.sessions() as reader:
        event_id = await reader.scalar(select(ChatEventModel.id))
    identity = await test_case.repository.enqueue(
        test_case.control.lease.conversation_id,
        1,
        "resume-steer",
        kind="message",
        event_id=event_id,
        work_id=test_case.control.current["id"],
        ready=False,
    )
    await test_case.repository.prepare_input(identity, {"text": "现在怎样了？"})
    test_case.runtime = replace(test_case.runtime, max_model_requests=1)
    first = await run(test_case)
    assert first.work_state == "queued"
    assert (
        sum(
            "原 Work 新输入的答复机会" in (message.content or "")
            for message in test_case.provider.requests[0].messages
        )
        == 1
    )
    recovered = WorkControl(
        test_case.repository,
        test_case.control.lease,
        test_case.control.source_key,
        test_case.control.source,
        test_case.control.validate,
    )
    recovered.current = await test_case.repository.get(first.work_id)
    test_case.control = recovered
    test_case.runtime = replace(test_case.runtime, work_control=recovered, max_model_requests=4)
    second = await run(test_case)
    assert second.work_state == "completed"
    assert (
        sum(
            "原 Work 新输入的答复机会" in (message.content or "")
            for message in test_case.provider.requests[-1].messages
        )
        == 0
    )
    assert "现在怎样了？" in "\n".join(
        message.content or "" for message in test_case.provider.requests[-1].messages
    )
    assert recovered.communication["input_feedback_through_id"] == identity
    assert await recovered.communication_reports(event_ids=(event_id,)) == []


@pytest.mark.asyncio
async def test_legacy_baseline_skips_old_consumed_but_keeps_new_pending(database, tmp_path):
    from sqlalchemy import update

    from qq_ai_bot.runtime.work_schema_v1 import work

    test_case = await case(
        database,
        tmp_path,
        [
            response(tool("write_fixture")),
            response(tool("task_control", {"action": "complete"})),
        ],
        reporting=None,
    )
    old_event = await new_event(test_case, "旧追问已按旧合同处理")
    old = await test_case.repository.enqueue(
        test_case.control.lease.conversation_id,
        1,
        "old-consumed",
        kind="message",
        event_id=old_event,
        work_id=test_case.control.current["id"],
        ready=False,
    )
    await test_case.repository.prepare_input(old, {"text": "旧追问已按旧合同处理"})
    await test_case.control.take_inputs("legacy")
    await test_case.control.confirm_inputs()
    async with database.immediate_session() as writer:
        await writer.execute(
            update(work)
            .where(work.c.id == test_case.control.current["id"])
            .values(checkpoint_json="{}")
        )
    test_case.control.current = await test_case.repository.get(test_case.control.current["id"])
    event_id = await new_event(test_case, "新增追问需要答复")
    identity = await test_case.repository.enqueue(
        test_case.control.lease.conversation_id,
        1,
        "new-pending",
        kind="message",
        event_id=event_id,
        work_id=test_case.control.current["id"],
        ready=False,
    )
    await test_case.repository.prepare_input(identity, {"text": "新增追问需要答复"})
    result = await run(test_case)
    assert result.work_state == "completed"
    opportunities = [
        message.content
        for message in test_case.provider.requests[-1].messages
        if "原 Work 新输入的答复机会" in (message.content or "")
    ]
    assert len(opportunities) == 1 and f"event_ids=[{event_id}]" in opportunities[0]
    assert f"event_ids=[{old_event}]" not in opportunities[0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "delivery",
    [
        "rejected",
        "other_target",
        "current_target",
        "failed",
        "unknown",
        "reported_success",
        "reported_failed",
        "reported_unknown",
    ],
)
async def test_only_actual_related_send_suppresses_stage_opportunity(database, tmp_path, delivery):
    test_case = await case(database, tmp_path, [])
    if delivery == "rejected":
        send_args = {
            "text": "未执行",
            "work_report": {"kind": "reply", "reply_to_event_ids": [999999]},
        }
    elif delivery.startswith("reported_"):
        send_args = {"text": "阶段结果", "work_report": {"kind": "progress"}}
    else:
        send_args = {
            "text": "阶段结果",
            "target": {"kind": "space", "id": test_case.env.space}
            if delivery in {"current_target", "failed", "unknown"}
            else {"kind": "person", "id": "different-person"},
        }
    scripted = iter(
        [
            response(tool("send_message", START, "start")),
            ChatResponse(
                "这里有实质发现。",
                0,
                tool_calls=(tool("write_fixture"), tool("send_message", send_args, "stage")),
            ),
            response(
                tool(
                    "task_control",
                    {"action": "fail", "reason": "交付受阻"}
                    if delivery in {"failed", "unknown", "reported_failed", "reported_unknown"}
                    else {"action": "complete"},
                )
            ),
        ]
    )
    test_case.provider._responder = lambda _: next(scripted)
    if delivery in {"failed", "unknown", "reported_failed", "reported_unknown"}:
        original_execute = test_case.backend.execute_call

        async def execute(invocation):
            name = invocation.call.function.name
            arguments = invocation.call.function.arguments
            result = await original_execute(invocation)
            if name == "send_message" and json.loads(arguments).get("text") == "阶段结果":
                return json.dumps(
                    {
                        "ok": False,
                        "data": {
                            "status": delivery.removeprefix("reported_"),
                            "target": {"kind": "space", "id": test_case.env.space},
                        },
                    }
                )
            return result

        test_case.backend.execute_call = execute
    result = await run(test_case)
    assert (result.work_state == "completed") == (
        delivery not in {"failed", "unknown", "reported_failed", "reported_unknown"}
    )
    stage = [
        message
        for message in test_case.provider.requests[-1].messages
        if "上一段工具结果仍是内部资料" in (message.content or "")
    ]
    assert bool(stage) == (delivery != "reported_success")
    assert test_case.control.tools_started == (2 if delivery == "rejected" else 3)


@pytest.mark.asyncio
async def test_pause_replay_defers_new_input_and_feedback_without_losing_it(database, tmp_path):
    from qq_ai_bot.domain.messages import ModelResponseStatus, ProviderContinuation

    checkpoint = ProviderContinuation(
        provider="fake", protocol="chat_completions", payload={"paused": "search"}
    )
    test_case = await case(
        database,
        tmp_path,
        [
            ChatResponse(
                "",
                0,
                status=ModelResponseStatus.INCOMPLETE,
                incomplete_reason="pause_turn",
                continuation=checkpoint,
            ),
            response(tool("read_fixture")),
            response(tool("write_fixture")),
            response(tool("task_control", {"action": "complete"})),
        ],
        reporting="quiet",
    )
    event_id = await new_event(test_case, "暂停期间的追问")
    original_complete = test_case.provider.complete

    async def complete(request):
        answer = await original_complete(request)
        if len(test_case.provider.requests) == 1:
            identity = await test_case.repository.enqueue(
                test_case.control.lease.conversation_id,
                1,
                "pause-steer",
                kind="message",
                event_id=event_id,
                work_id=test_case.control.current["id"],
                ready=False,
            )
            await test_case.repository.prepare_input(identity, {"text": "暂停期间的追问"})
        return answer

    test_case.provider.complete = complete
    result = await run(test_case)
    assert result.work_state == "completed"
    replay = test_case.provider.requests[1]
    assert replay.messages == test_case.provider.requests[0].messages
    assert replay.continuation == checkpoint and replay.continuation_items == ()
    assert "暂停期间的追问" in str(test_case.provider.requests[2].continuation_items)
    assert test_case.observed == ["write_fixture"]
    assert "provider_pause_replay" not in test_case.control.session.progress
