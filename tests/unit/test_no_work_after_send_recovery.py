"""A neutral WorkControl is ordinary chat after a confirmed Social delivery."""

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from tests.conftest import MemorySender, build_harness, make_settings
from tests.support.commands_and_chat_helpers import inbound
from tests.support.correctness_wire import KINDS, wire
from tests.support.fixed_contract_fixture import bind_main_contract
from tests.support.social_identity_cases import social_env

from yuki.conversation.hydrate import require_primary_alias_for_conversation
from yuki.domain.messages import ChatResponse, FunctionCallOutput, ToolCall, ToolFunction
from yuki.llm.base import LLMMalformedFunctionCallError
from yuki.llm.fake import FakeLLMProvider
from yuki.model_runtime.db_models import ModelInvocationModel
from yuki.model_runtime.repository import ModelInvocationRepository
from yuki.persistence.models import ChatEventModel
from yuki.runtime.work_activation import current_work_control
from yuki.runtime.work_schema_v1 import work


@pytest.mark.asyncio
@pytest.mark.parametrize("accepted,work_enabled", [(False, False), (False, True), (True, True)])
@pytest.mark.parametrize(
    "protocol,ending", [(kind, "empty") for kind in ["fake", *KINDS]] + [("fake", "malformed")]
)
async def test_confirmed_send_empty_response_respects_actual_work_ownership(
    database, tmp_path, accepted, work_enabled, protocol, ending
):
    env = await social_env(database, tmp_path)
    async with database.read_session() as reader:
        await require_primary_alias_for_conversation(reader, env.context.conversation_id)
    steps = iter(
        [
            *(
                [
                    (
                        "manage_task",
                        {"action": "start", "goal": "检查"},
                    )
                ]
                if accepted
                else []
            ),
            (
                "send",
                {
                    "text": "已确认的原消息",
                },
            ),
            (ending, {}),
        ]
    )
    ownership = []

    def respond(request):
        control = current_work_control.get()
        assert (control is not None) == work_enabled
        ownership.append(control is not None and control.current is not None)
        name, arguments = next(steps)
        if name in {"empty", "malformed"}:
            receipts = [message.content for message in request.messages if message.role == "tool"]
            receipts.extend(
                item.output
                for item in request.continuation_items
                if isinstance(item, FunctionCallOutput)
            )
            assert any('"succeeded"' in str(receipt) for receipt in receipts), receipts
            if name == "malformed":
                raise LLMMalformedFunctionCallError("malformed call after a successful delivery")
            return "" if protocol == "fake" else ChatResponse("", 0)
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
        make_settings(database.url, runtime_work_enabled=work_enabled, enabled_groups_csv="20001"),
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
    chat = harness.processor._chat
    client, captured = None, []
    if protocol != "fake":
        client, captured = wire(
            SimpleNamespace(provider=provider, runner=chat.runtime.runner), protocol
        )
        chat._models = chat.runtime.runner._models
    chat.runtime.runner._models._invocations = ModelInvocationRepository(database)
    try:
        result = await harness.processor.handle(message, sender)
    finally:
        if client is not None:
            await client.aclose()
    assert result.reason == ("chat" if accepted or ending == "empty" else "llm_failure")
    assert not sender.messages
    assert len(provider.requests) == (3 if accepted else 2)
    assert ownership == ([False, True, True] if accepted else [False, False])
    assert sum(action == "send_group_msg" for action, _ in env.bot.calls) == 1
    async with database.read_session() as reader:
        outgoing = (
            await reader.scalars(
                select(ChatEventModel).where(
                    ChatEventModel.canonical_conversation_id == env.context.conversation_id,
                    ChatEventModel.direction == "outbound",
                )
            )
        ).all()
        works = (await reader.execute(select(work))).mappings().all()
        invocations = (await reader.scalars(select(ModelInvocationModel))).all()
    assert sum(row.content == "已确认的原消息" for row in outgoing) == 1
    assert len(outgoing) == 1
    if accepted:
        assert len(works) == 1
        assert works[0]["state"] == ("completed" if ending == "empty" else "queued")
        assert works[0]["model_requests"] == 3 and works[0]["sent_messages"] == 1
    else:
        assert not works
    assert provider.requests[1].tools == provider.requests[0].tools
    assert provider.requests[1].messages[: len(provider.requests[0].messages)] == (
        provider.requests[0].messages
    )
    if captured:
        assert len(invocations) == len(provider.requests)
        assert all(row.success for row in invocations)
        assert sum(row.physical_request_count for row in invocations) == len(captured)
    artifact = Path("test-results/empty-model-final") / (
        f"{protocol}-{ending}-{accepted}-{work_enabled}.json"
    )
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text(
        json.dumps(
            {
                "protocol": protocol,
                "ending": ending,
                "requests": captured,
                "model_requests": len(provider.requests),
                "result": result.reason,
                "deliveries": [row.content for row in outgoing],
                "work_states": [row["state"] for row in works],
                "invocations": [
                    {"success": row.success, "physical_requests": row.physical_request_count}
                    for row in invocations
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
