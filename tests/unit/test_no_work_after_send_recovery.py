"""A neutral WorkControl is ordinary chat after a confirmed Social delivery."""

import json
from dataclasses import replace

import pytest
from sqlalchemy import select
from tests.conftest import MemorySender, build_harness, make_settings
from tests.support.fixed_contract_fixture import bind_main_contract
from tests.support.social_identity_cases import social_env
from tests.unit.test_commands_and_chat import inbound

from qq_ai_bot.conversation.hydrate import require_primary_alias_for_conversation
from qq_ai_bot.domain.messages import ChatResponse, ToolCall, ToolFunction
from qq_ai_bot.llm.base import LLMEmptyResponseError
from qq_ai_bot.llm.fake import FakeLLMProvider
from qq_ai_bot.persistence.models import ChatEventModel
from qq_ai_bot.runtime.work_activation import current_work_control
from qq_ai_bot.runtime.work_schema_v1 import work


@pytest.mark.asyncio
@pytest.mark.parametrize("accepted", [False, True])
async def test_confirmed_send_empty_response_respects_actual_work_ownership(
    database, tmp_path, accepted
):
    env = await social_env(database, tmp_path)
    async with database.sessions() as reader:
        primary_alias = await require_primary_alias_for_conversation(
            reader, env.context.conversation_id
        )
    steps = iter(
        [
            *(
                [
                    (
                        "task_control",
                        {
                            "action": "accept",
                            "goal": "检查",
                            "output_kind": "answer",
                            "reporting": "interactive",
                        },
                    )
                ]
                if accepted
                else []
            ),
            (
                "send_message",
                {
                    "text": "已确认的原消息",
                    **({"work_report": {"kind": "start"}} if accepted else {}),
                },
            ),
            ("empty", {}),
            *(
                [
                    ("body", {}),
                    ("task_control", {"action": "fail", "reason": "仍有余项，明确停止"}),
                    ("final", {}),
                ]
                if accepted
                else []
            ),
        ]
    )
    ownership = []

    def respond(request):
        control = current_work_control.get()
        assert control is not None
        ownership.append(control.current is not None)
        name, arguments = next(steps)
        if name == "empty":
            receipts = [message.content for message in request.messages if message.role == "tool"]
            assert any('"succeeded"' in str(receipt) for receipt in receipts), receipts
            raise LLMEmptyResponseError("empty after a real successful delivery")
        if name == "body":
            return ChatResponse("这仍然只是内部阶段结果", 0)
        if name == "final":
            return ChatResponse("NO_REPLY", 0)
        return ChatResponse(
            "",
            0,
            tool_calls=(
                ToolCall(str(len(provider.requests)), ToolFunction(name, json.dumps(arguments))),
            ),
        )

    provider = FakeLLMProvider(respond)
    harness = build_harness(
        database,
        make_settings(database.url, runtime_work_enabled=True, enabled_groups_csv="20001"),
        provider,
    )
    bind_main_contract(harness, tmp_path)
    harness.processor._chat._tools.social_service = env.service
    message = replace(
        inbound(
            "检查并告诉我",
            message_id="after-send-empty",
            user_id="10001",
            group_id="20001",
            mentions_bot=True,
        ),
        bot_user_id="80001",
        conversation_id=env.context.conversation_id,
        legacy_conversation_key=primary_alias,
        person_id=env.person,
        space_id=env.space,
        presence_id=env.presence,
    )
    sender = MemorySender()
    result = await harness.processor.handle(message, sender)
    assert result.reason == "chat"
    assert not sender.messages
    assert len(provider.requests) == (6 if accepted else 2)
    assert ownership == ([False, True, True, True, True, True] if accepted else [False, False])
    assert sum(action == "send_group_msg" for action, _ in env.bot.calls) == 1
    async with database.sessions() as reader:
        outgoing = (
            await reader.scalars(
                select(ChatEventModel).where(
                    ChatEventModel.canonical_conversation_id == env.context.conversation_id,
                    ChatEventModel.direction == "outbound",
                )
            )
        ).all()
        works = (await reader.execute(select(work))).mappings().all()
    assert len(outgoing) == 1 and outgoing[0].content == "已确认的原消息"
    if accepted:
        assert len(works) == 1 and works[0]["state"] == "failed"
        assert works[0]["model_requests"] == 6 and works[0]["sent_messages"] == 1
        assert "不能据此结束交互式 Work" in str(provider.requests[-2].messages)
    else:
        assert not works
    assert provider.requests[1].tools == provider.requests[0].tools
    assert provider.requests[1].messages[: len(provider.requests[0].messages)] == (
        provider.requests[0].messages
    )
