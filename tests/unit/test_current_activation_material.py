"""Actual Main requests keep runtime material inside the current activation."""

import json
from dataclasses import replace

import httpx
import pytest
from sqlalchemy import select, update
from tests.conftest import MemorySender
from tests.support.background_authority import approve_background_plugin
from tests.unit.test_history_dispatch_ownership import _scene

from qq_ai_bot.conversation.observation_models import ContextObservationModel, ContextSelectionModel
from qq_ai_bot.conversation.projection_models import PromptProjectionModel
from qq_ai_bot.conversation.scope import ConversationTurnSnapshot
from qq_ai_bot.domain.conversations import ConversationScope
from qq_ai_bot.domain.messages import (
    ChatImage,
    ChatMessage,
    ChatRequest,
    ChatResponse,
    SenderIdentity,
    ToolCall,
    ToolFunction,
)
from qq_ai_bot.event_prompt import ChatEventPromptRenderer
from qq_ai_bot.identity.canonical_repository import ensure_person
from qq_ai_bot.llm.fake import FakeLLMProvider
from qq_ai_bot.llm.openai_compatible import OpenAICompatibleProvider
from qq_ai_bot.persistence.models import ChatEventModel
from qq_ai_bot.runtime.origin import TurnOrigin
from qq_ai_bot.runtime.protocol_store import ProtocolStore
from qq_ai_bot.runtime.trigger import ExternalEventTurnTrigger, SandboxTaskTurnTrigger
from qq_ai_bot.runtime.work_activation import current_work_control
from qq_ai_bot.runtime.work_schema_v1 import journal
from qq_ai_bot.runtime.work_source_guard import WorkSourceGuard
from qq_ai_bot.services.agent_tools import ToolRuntime
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
        assert (provider.requests[-1].messages[-1].content or "").endswith(f"继续{number}")
        assert current.index("当前工作：") < current.index(f"继续{number}")
        if value:
            assert current.count(value) == 1
            assert current.index(value) < current.index(f"继续{number}")
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
    first_saved = await _saved(database)
    await harness.processor.handle(
        replace(message, message_id="same-person-next", text="同一人的第二条原输入"), MemorySender()
    )
    frozen = await _saved(database)
    adopted = json.loads(frozen.payload_json)
    assert adopted[: len(json.loads(first_saved.payload_json))] == json.loads(
        first_saved.payload_json
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
    latest = await _saved(database)
    assert latest.view_key == frozen.view_key == message.conversation_id
    assert latest.epoch_id == frozen.epoch_id == first_saved.epoch_id
    assert json.loads(latest.payload_json)[: len(adopted)] == adopted
    request_contents = [item.content for item in provider.requests[-1].messages]
    for item in adopted:
        assert request_contents.count(item["message"]["content"]) == 1
    await _assert_public_chat_only(database)


@pytest.mark.parametrize("evicted", [False, True])
async def test_repeated_source_keeps_entire_adopted_group_and_current_activation(
    database, tmp_path, evicted
):
    provider = FakeLLMProvider(lambda request: "内部结束")
    _, harness, chat, _, message = await _scene(database, tmp_path, provider)
    observed = [
        replace(message, message_id="group-first", text="第一条原输入", mentions_bot=False),
        replace(message, message_id="group-second", text="第二条原输入", mentions_bot=False),
    ]
    for inbound in observed:
        result = await harness.processor.handle(inbound, MemorySender())
        assert result.reason == "group_observed"
    captured = []
    compose = chat.runtime.main_turns.compose

    async def capture(**kwargs):
        captured.append(kwargs)
        return await compose(**kwargs)

    chat.runtime.main_turns.compose = capture
    await harness.processor.handle(message, MemorySender())
    first = await _saved(database)
    adopted = json.loads(first.payload_json)
    async with database.sessions() as reader:
        event_ids = list(
            await reader.scalars(
                select(ChatEventModel.id)
                .where(ChatEventModel.platform_message_id.in_([m.message_id for m in observed]))
                .order_by(ChatEventModel.id)
            )
        )
        original_selected = [
            (row.id, row.payload_json)
            for row in await reader.scalars(
                select(ContextSelectionModel).order_by(ContextSelectionModel.id)
            )
        ]
    group = next(item for item in adopted if set(event_ids) <= set(item["event_ids"]))
    repeated = await chat._ledger.get_event(event_ids[1])
    assert repeated is not None
    renderer = ChatEventPromptRenderer(
        (repeated,),
        timezone=harness.settings.default_timezone,
        bot_display_name=harness.settings.bot_display_name,
    )
    current = renderer.reference_message(repeated, current_event_id=repeated.id)
    if evicted:
        await chat.runtime.main_turns._projections.invalidate_view(
            message.conversation_id, reason="capacity"
        )
    kwargs = dict(captured[0])
    kwargs["inbound"] = observed[1]
    kwargs["context"] = replace(
        kwargs["context"],
        current_event_id=repeated.id,
        current_message=current,
        history_fragments=tuple(
            (ids, ChatMessage("user", "未采用的重新分组候选"))
            for ids, _ in kwargs["context"].history_fragments
        ),
        history_event_fragments=tuple(
            (ids, ChatMessage("user", "未采用的单事件候选"))
            for ids, _ in kwargs["context"].history_event_fragments
        ),
    )
    composition = await compose(**kwargs)
    with validating_request(TranscriptRequest(composition.messages, None, ())):
        await composition.commit_projection()
        await provider.complete(ChatRequest(messages=composition.messages, model="fake"))
    repeated_request = provider.requests[-1]
    assert group["message"]["content"] in [m.content for m in repeated_request.messages]
    assert (repeated_request.messages[-1].content or "").endswith(current.content)
    saved = await _saved(database)
    assert saved.payload_json == first.payload_json
    assert (saved.epoch_id != first.epoch_id) is evicted
    assert saved.rebuild_reason == ("capacity" if evicted else first.rebuild_reason)
    async with database.sessions() as reader:
        assert [
            (row.id, row.payload_json)
            for row in await reader.scalars(
                select(ContextSelectionModel).order_by(ContextSelectionModel.id)
            )
        ] == original_selected
    await harness.processor.handle(
        replace(message, message_id="ordinary-new-source", text="真正的全新输入"), MemorySender()
    )
    latest = json.loads((await _saved(database)).payload_json)
    assert latest[: len(adopted)] == adopted
    assert sum("真正的全新输入" in item["message"]["content"] for item in latest) == 1
    assert group["message"]["content"] in [m.content for m in provider.requests[-1].messages]


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
            message.conversation_id, reason="capacity"
        )
    async with database.sessions() as reader:
        assert all(
            "activation-" not in row.payload_json
            for row in await reader.scalars(select(ContextSelectionModel))
        )


@pytest.mark.parametrize("entry", ["external", "sandbox"])
async def test_actorless_main_dispatch_keeps_trigger_temporary_and_original_chat_public(
    database, tmp_path, entry
):
    provider = FakeLLMProvider(lambda request: "内部结束")
    env, harness, chat, _, message = await _scene(database, tmp_path, provider)
    scope = ConversationScope.group("80001", "20001")
    intent = "完整  意图\n" * 400 + "INTENT_LAST_LINE"
    completion = "完成回执  原文\nCOMPLETION_LAST_LINE"
    if entry == "external":
        await approve_background_plugin(
            database,
            plugin_id="test.common-external",
            bot_user_id="80001",
            group_id="20001",
            creator_user_id="10001",
        )
        body = "通知原文  保留空白\n" * 700 + "NOTIFICATION_LAST_LINE"
        created = await env.service.writer.append_external(
            scope=scope,
            platform_message_id="old-external-source",
            source_plugin_id="test.common-external",
            external_source="offline-fixture",
            external_event_key="original-source",
            external_event_type="source.ready",
            external_payload={"id": "original-source"},
            external_target_id="20001",
            content=body,
            occurred_at=chat._time.current_default().utc,
        )
        trigger = ExternalEventTurnTrigger(
            plugin_id="test.common-external",
            source_event_id=created.event.id,
            target_type="group",
            target_id="20001",
            agent_intent=intent,
        )
        await harness.processor.handle(message, MemorySender())
        first = await _saved(database)
        old = first.payload_json
        await chat.runtime.main_turns._projections.invalidate_view(
            message.conversation_id, reason="capacity"
        )
        source_runtime = None
    else:
        body = "最初用户正文  保留\nORIGINAL_USER_LAST_LINE"
        created = await env.service.writer.append(
            scope=scope,
            platform_message_id="original-sandbox-user-source",
            sender_user_id="10001",
            direction="inbound",
            content=body,
        )
        trigger = SandboxTaskTurnTrigger(
            source_event_id=created.event.id,
            target_type="group",
            target_id="20001",
            agent_intent=intent,
            completion_payload=completion,
        )
        source_runtime = ToolRuntime(
            inbound=replace(
                message,
                message_id="original-sandbox-user-source",
                text=body,
                source_event_id=created.event.id,
            ),
            gateway=env.bot,
            allow_generic_onebot=False,
            runtime_config=await chat._runtime_config.snapshot(user_id="10001", group_id="20001"),
            conversation_key=f"canonical:{message.conversation_id}:generation:1",
            conversation_id=message.conversation_id,
            person_id=env.person,
            space_id=env.space,
            origin=TurnOrigin.USER_MESSAGE,
            read_scope=scope,
            trigger_event_id=created.event.id,
            trigger_message_id="original-sandbox-user-source",
        )
        old = None
    event = await harness.ledger.get_event(created.event.id)
    state = await harness.conversation_scopes.get(scope)
    config = await chat._runtime_config.snapshot(group_id="20001")
    first_wakeup_call = len(provider.requests)
    dispatched_controls = []
    source_checks = []

    async def check_original_source():
        control = current_work_control.get()
        if (
            control is not None
            and control.current is not None
            and control.session is not None
            and control.session.source_guard is not None
        ):
            guard = control.session.source_guard
            assert event.id in set(guard.version.visible_event_ids) | set(guard.additional_events)
            assert await guard.check(control)
            source_checks.append(guard.snapshot())

    def finish_actual_work(request):
        dispatched_controls.append(current_work_control.get())
        action = (
            {"action": "accept", "goal": "只核原始来源，完成后结束"}
            if len(provider.requests) == first_wakeup_call + 1
            else {"action": "complete", "result": "原来源已核"}
        )
        return ChatResponse(
            "",
            0,
            tool_calls=(
                ToolCall(
                    f"source-{len(provider.requests)}",
                    ToolFunction("task_control", json.dumps(action)),
                ),
            ),
        )

    provider._responder = finish_actual_work
    async with chat._turn_coordinator.background_turn(scope.key) as token:
        snapshot = ConversationTurnSnapshot(
            state.id,
            scope.key,
            state.generation,
            event.id,
            token.version,
            transport_scope_key=scope.key,
        )
        await chat.generate_main_agent_wakeup(
            event=event,
            trigger=trigger,
            identity=scope,
            runtime=config,
            turn_token=token,
            turn_snapshot=snapshot,
            gateway=env.bot,
            presence_id=env.presence,
            space_id=env.space,
            conversation_id=message.conversation_id,
            before_model_request=check_original_source,
            source_runtime=source_runtime,
        )
    current = provider.requests[first_wakeup_call].messages[-1].content
    payload = json.loads(current.rsplit("\n", 1)[1])
    assert payload["agent_intent"] == intent
    if entry == "external":
        assert payload["summary"] == body
    else:
        assert payload["completion"] == completion and payload["source_event_id"] == event.id
        assert "summary" not in payload and body not in current
    stored = await _saved(database)
    assert "INTENT_LAST_LINE" not in stored.payload_json
    assert "COMPLETION_LAST_LINE" not in stored.payload_json
    assert "NOTIFICATION_LAST_LINE" not in stored.payload_json
    if old is not None:
        assert stored.payload_json == old
        assert json.loads(old)[-1]["message"]["content"] in [
            item.content for item in provider.requests[-1].messages
        ]
    else:
        adopted = [
            item for item in json.loads(stored.payload_json) if event.id in item["event_ids"]
        ]
        assert len(adopted) == 1 and body in adopted[0]["message"]["content"]
    original_control = dispatched_controls[-1]
    assert original_control is not None and original_control.current is not None
    assert original_control.current["state"] == "completed"
    assert original_control.current["model_requests"] == 2
    async with database.sessions() as reader:
        original_journal = await reader.scalar(
            select(journal.c.payload_json).where(
                journal.c.work_id == original_control.current["id"]
            )
        )
    private = await ProtocolStore(database).hydrate(json.loads(original_journal))
    guard = WorkSourceGuard.restore(private["metadata"]["source_guard"])
    assert event.id in set(guard.version.visible_event_ids) | set(guard.additional_events)
    if entry == "external":
        assert event.id in private["metadata"]["event_ids"]
    assert source_checks and guard.fingerprint is not None
    provider._responder = lambda request: "内部结束"
    await harness.processor.handle(
        replace(message, message_id="ordinary-after-host-trigger", text="唤醒之后的普通当前消息"),
        MemorySender(),
    )
    following = "\n".join(item.content or "" for item in provider.requests[-1].messages)
    assert "INTENT_LAST_LINE" not in following
    assert "COMPLETION_LAST_LINE" not in following
    assert "NOTIFICATION_LAST_LINE" not in following
    async with database.immediate_session() as writer:
        await writer.execute(
            update(ChatEventModel)
            .where(ChatEventModel.id == event.id)
            .values(content="实际来源已改变")
        )
    assert not await guard.check(original_control)
    async with database.sessions() as reader:
        assert (
            await reader.scalar(
                select(journal.c.payload_json).where(
                    journal.c.work_id == original_control.current["id"]
                )
            )
            == original_journal
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


async def test_image_activation_keeps_shared_prefix_and_current_pixels(database, tmp_path):
    provider = FakeLLMProvider(lambda request: "内部结束")
    _, harness, chat, _state, message = await _scene(database, tmp_path, provider)
    await harness.processor.handle(message, MemorySender())
    first = await _saved(database)
    adopted = json.loads(first.payload_json)
    image = ChatImage("data:image/png;base64,aW1hZ2U=")
    compose = chat.runtime.main_turns.compose

    async def with_current_pixels(**kwargs):
        context = kwargs["context"]
        kwargs["context"] = replace(
            context,
            current_message=replace(
                context.current_message,
                images=(image,),
            ),
        )
        return await compose(**kwargs)

    chat.runtime.main_turns.compose = with_current_pixels
    await harness.processor.handle(
        replace(message, message_id="image-current", text="这是当前图片附件"), MemorySender()
    )
    actual = provider.requests[-1].messages
    assert [m.images for m in actual if m.images] == [(image,)]
    assert actual[-1].images == (image,)
    current = await _saved(database)
    assert current.view_key == first.view_key == message.conversation_id
    assert current.epoch_id == first.epoch_id
    assert json.loads(current.payload_json)[: len(adopted)] == adopted
    assert image.data_url not in current.payload_json
    chat.runtime.main_turns.compose = compose
    await harness.processor.handle(
        replace(message, message_id="after-image", text="继续原聊天"), MemorySender()
    )
    assert not any(m.images for m in provider.requests[-1].messages)
    assert (await _saved(database)).epoch_id == first.epoch_id
    assert json.loads((await _saved(database)).payload_json)[: len(adopted)] == adopted
