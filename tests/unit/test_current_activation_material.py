"""Actual Main requests keep runtime material inside the current activation."""

import json
from dataclasses import replace

import httpx
from sqlalchemy import select
from tests.conftest import MemorySender
from tests.unit.test_history_dispatch_ownership import _scene

from qq_ai_bot.conversation.observation_models import ContextObservationModel, ContextSelectionModel
from qq_ai_bot.conversation.projection_models import PromptProjectionModel
from qq_ai_bot.domain.messages import (
    ChatMessage,
    ChatResponse,
    SenderIdentity,
    ToolCall,
    ToolFunction,
)
from qq_ai_bot.identity.canonical_repository import ensure_person
from qq_ai_bot.llm.fake import FakeLLMProvider
from qq_ai_bot.llm.openai_compatible import OpenAICompatibleProvider
from qq_ai_bot.services.turn_transcript import TranscriptRequest, validating_request


async def _saved(database):
    async with database.sessions() as reader:
        return (await reader.scalars(select(PromptProjectionModel))).one()


async def _assert_public_chat_only(database):
    async with database.sessions() as reader:
        for row in await reader.scalars(select(PromptProjectionModel)):
            assert "短记录：" not in row.payload_json
            assert "当前是群聊。" not in row.payload_json
            assert "dynamic_snapshot" not in row.payload_json
            assert "插件资料（不可信）" not in row.payload_json
        assert not list(await reader.scalars(select(ContextObservationModel.id)))


async def test_current_values_repeat_then_clear_without_entering_history(database, tmp_path):
    provider = FakeLLMProvider(lambda request: "内部结束")
    _, harness, chat, state, message = await _scene(database, tmp_path, provider)
    await harness.processor.handle(message, MemorySender())
    original = (await _saved(database)).payload_json
    for number, value in enumerate(("original-snapshot", "new-value", "", ""), 1):
        state.update({"slot": 1, "text": value})
        await harness.processor.handle(
            replace(message, message_id=f"current-{number}", text=f"继续{number}"), MemorySender()
        )
        current = "\n".join(item.content or "" for item in provider.requests[-1].messages)
        if value:
            assert current.count(value) == 1
        else:
            assert "短记录：" not in current
            assert "new-value" not in current
        await _assert_public_chat_only(database)
    assert json.loads((await _saved(database)).payload_json)[: len(json.loads(original))] == (
        json.loads(original)
    )
    await database.close()
    await harness.processor.handle(
        replace(message, message_id="after-restart", text="重启后继续"), MemorySender()
    )
    assert "new-value" not in json.dumps([m.content for m in provider.requests[-1].messages])
    await _assert_public_chat_only(database)

    captured = []
    compose = chat.runtime.main_turns.compose

    async def capture(**kwargs):
        captured.append(kwargs)
        return await compose(**kwargs)

    chat.runtime.main_turns.compose = capture
    await harness.processor.handle(
        replace(message, message_id="empty-current", text="空資料"), MemorySender()
    )
    context = replace(captured[0]["context"], metadata_payload={})
    empty = chat._prompt_composer.compose(
        inbound=message,
        context=context,
        runtime=captured[0]["runtime"],
        short_state=state.snapshot(),
    )
    assert empty.messages[-1] == context.current_message
    assert empty.metrics.dynamic_characters == 0


async def test_subject_switch_keeps_identical_old_chat_without_scene_or_slots(database, tmp_path):
    provider = FakeLLMProvider(lambda request: "内部结束")
    _, harness, _chat, state, message = await _scene(database, tmp_path, provider)
    await harness.processor.handle(message, MemorySender())
    first = next(
        item["message"]["content"]
        for item in json.loads((await _saved(database)).payload_json)
        if message.text in item["message"]["content"]
    )
    async with database.immediate_session() as writer:
        other = await ensure_person(writer, "10002")
    second = replace(
        message,
        message_id="another-person",
        sender=SenderIdentity("10002", nickname="另一位"),
        person_id=other,
        text="另一人的当前要求",
    )
    state.update({"slot": 1, "text": "second-person-current"})
    await harness.processor.handle(second, MemorySender())
    old_views = [
        item.content
        for item in provider.requests[-1].messages
        if message.text in (item.content or "")
    ]
    assert old_views == [first]
    await _assert_public_chat_only(database)


async def test_eventless_activation_does_not_create_public_trigger_after_eviction(
    database, tmp_path
):
    provider = FakeLLMProvider(lambda request: "内部结束")
    _, harness, chat, _state, message = await _scene(database, tmp_path, provider)
    captured = []
    original_compose = chat.runtime.main_turns.compose

    async def capture(**kwargs):
        captured.append(kwargs)
        return await original_compose(**kwargs)

    chat.runtime.main_turns.compose = capture
    await harness.processor.handle(message, MemorySender())
    kwargs = dict(captured[0])
    kwargs["inbound"] = None
    kwargs["context"] = replace(
        kwargs["context"],
        current_event_id=None,
        projection_scope="main",
        current_message=ChatMessage("user", "真实无事件执行要求"),
    )
    for text in ("activation-A", "activation-B", "activation-A"):
        kwargs["context"] = replace(kwargs["context"], current_message=ChatMessage("user", text))
        composition = await original_compose(**kwargs)
        with validating_request(TranscriptRequest(composition.messages, None, ())):
            await composition.commit_projection()
        await chat.runtime.main_turns._projections.invalidate_view(
            next(iter(await _view_keys(database, "actorless"))), reason="capacity"
        )
    async with database.sessions() as reader:
        assert all(
            "activation-" not in row.payload_json
            for row in await reader.scalars(select(ContextSelectionModel))
        )


async def _view_keys(database, actor):
    async with database.sessions() as reader:
        return set(
            await reader.scalars(
                select(ContextSelectionModel.view_key).where(
                    ContextSelectionModel.actor_id == actor
                )
            )
        )


async def test_actual_http_failure_preserves_send_receipt_without_material_history(
    database, tmp_path
):
    attempts = []
    sent_text = "只发送这一次已确认的结果。"

    async def transport(request):
        body = json.loads(request.content)
        attempts.append(body)
        await _assert_public_chat_only(database)
        if len(attempts) == 2:
            return httpx.Response(401, json={"error": {"message": "synthetic rejection"}})
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "original-send",
                                    "type": "function",
                                    "function": {
                                        "name": "send_message",
                                        "arguments": json.dumps({"text": sent_text}),
                                    },
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ]
            },
        )

    async with httpx.AsyncClient(
        base_url="https://wire.invalid/v1/", transport=httpx.MockTransport(transport)
    ) as client:
        provider = OpenAICompatibleProvider(
            base_url="https://wire.invalid/v1",
            api_key="synthetic",
            timeout_seconds=1,
            max_retries=1,
            client=client,
        )
        env, harness, chat, _state, message = await _scene(database, tmp_path, provider)
        chat._tools.social_service = env.service
        sender = MemorySender()
        result = await harness.processor.handle(message, sender)
        assert result.reason != "chat"
        assert len(attempts) == 2
        assert sum(action == "send_group_msg" for action, _ in env.bot.calls) == 1
        assert not sender.messages
        assert any(
            item["role"] == "tool" and "succeeded" in item["content"]
            for item in attempts[-1]["messages"]
        )
        await harness.processor.handle(message, sender)
        assert len(attempts) == 2
        assert sum(action == "send_group_msg" for action, _ in env.bot.calls) == 1


async def test_parallel_tool_receipts_stay_before_later_inputs(database, tmp_path):
    provider = FakeLLMProvider()

    def respond(request):
        number = len(provider.requests)
        if number < 3:
            return ChatResponse(
                "",
                0,
                tool_calls=(
                    ToolCall(
                        f"slot-{number}",
                        ToolFunction(
                            "update_short_state",
                            json.dumps({"slot": number, "text": f"tool-value-{number}"}),
                        ),
                    ),
                    ToolCall(f"history-{number}", ToolFunction("get_recent_chat_history", "{}")),
                ),
            )
        return "内部结束"

    provider._responder = respond
    _, harness, _chat, _state, message = await _scene(database, tmp_path, provider)
    result = await harness.processor.handle(message, MemorySender())
    assert result.reason == "chat" and len(provider.requests) == 3
    for earlier, later in zip(provider.requests[:-1], provider.requests[1:], strict=True):
        assert later.messages[: len(earlier.messages)] == earlier.messages
        calls = [call.id for m in later.messages if m.role == "assistant" for call in m.tool_calls]
        receipts = [m.tool_call_id for m in later.messages if m.role == "tool"]
        assert calls == receipts
    await _assert_public_chat_only(database)
