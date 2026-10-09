"""A neutral WorkControl is ordinary chat after a confirmed Social delivery."""

import json
from dataclasses import replace

import pytest
from sqlalchemy import select
from tests.conftest import MemorySender, build_harness, make_settings
from tests.support.commands_and_chat_helpers import inbound
from tests.support.fixed_contract_fixture import bind_main_contract
from tests.support.social_identity_cases import social_env

from qq_ai_bot.conversation.hydrate import require_primary_alias_for_conversation
from qq_ai_bot.domain.messages import ChatResponse, ToolCall, ToolFunction
from qq_ai_bot.llm.base import LLMEmptyResponseError, LLMMalformedFunctionCallError
from qq_ai_bot.llm.fake import FakeLLMProvider
from qq_ai_bot.persistence.models import ChatEventModel
from qq_ai_bot.runtime.work_activation import current_work_control
from qq_ai_bot.runtime.work_schema_v1 import work


@pytest.mark.asyncio
@pytest.mark.parametrize("accepted", [False, True])
@pytest.mark.parametrize("failure", ["empty", "malformed"])
async def test_confirmed_send_empty_response_respects_actual_work_ownership(
    database, tmp_path, accepted, failure
):
    env = await social_env(database, tmp_path)
    async with database.sessions() as reader:
        await require_primary_alias_for_conversation(reader, env.context.conversation_id)
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
            (failure, {}),
        ]
    )
    ownership = []

    def respond(request):
        control = current_work_control.get()
        assert control is not None
        ownership.append(control.current is not None)
        name, arguments = next(steps)
        if name in {"empty", "malformed"}:
            receipts = [message.content for message in request.messages if message.role == "tool"]
            assert any('"succeeded"' in str(receipt) for receipt in receipts), receipts
            error = LLMEmptyResponseError if name == "empty" else LLMMalformedFunctionCallError
            raise error("unusable response after a real successful delivery")
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
        person_id=env.person,
        space_id=env.space,
        presence_id=env.presence,
    )
    sender = MemorySender()
    result = await harness.processor.handle(message, sender)
    assert result.reason == (
        "chat" if accepted else "empty_llm_response" if failure == "empty" else "llm_failure"
    )
    assert len(sender.messages) == (0 if accepted else 1)
    assert all(message.text != "已确认的原消息" for message in sender.messages)
    assert len(provider.requests) == (3 if accepted else 2)
    assert ownership == ([False, True, True] if accepted else [False, False])
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
    assert sum(row.content == "已确认的原消息" for row in outgoing) == 1
    assert len(outgoing) == 1 + len(sender.messages)
    if accepted:
        # The original confirmed send remains authoritative. An unusable
        # response suspends the accepted Work without buying a repair request.
        assert len(works) == 1 and works[0]["state"] == "suspended"
        assert works[0]["model_requests"] == 3 and works[0]["sent_messages"] == 1
    else:
        assert not works
    assert provider.requests[1].tools == provider.requests[0].tools
    assert provider.requests[1].messages[: len(provider.requests[0].messages)] == (
        provider.requests[0].messages
    )
