"""Actual Main candidates and dispatch outcomes retain only observed material."""

import json
import math
from dataclasses import replace
from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy import select, update
from tests.conftest import MemorySender
from tests.unit.test_history_dispatch_ownership import _scene
from tests.unit.test_runtime_material_delta import _changes, _saved

from qq_ai_bot.conversation.canonical_db_models import (
    CanonicalConversationModel,
    CanonicalConversationRollupModel,
)
from qq_ai_bot.conversation.projection_models import PromptProjectionModel
from qq_ai_bot.domain.messages import ChatMessage
from qq_ai_bot.llm.base import LLMError
from qq_ai_bot.llm.fake import FakeLLMProvider
from qq_ai_bot.llm.openai_compatible import OpenAICompatibleProvider
from qq_ai_bot.model_runtime.capacity import (
    ModelCapacity,
    estimate_request_tokens,
    estimate_text_tokens,
)
from qq_ai_bot.persistence.models import ChatEventModel


def _chat_reply(*, send_text=None):
    message = {"role": "assistant", "content": "内部收尾，无须额外发送。"}
    if send_text is not None:
        message.update(
            content="",
            tool_calls=[
                {
                    "id": "confirmed-boundary-send",
                    "type": "function",
                    "function": {
                        "name": "send_message",
                        "arguments": json.dumps({"text": send_text}),
                    },
                }
            ],
        )
    return {
        "choices": [{"message": message, "finish_reason": "tool_calls" if send_text else "stop"}],
        "usage": {"prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25},
    }


def _wire_current(body):
    return ChatMessage("user", body["messages"][-1]["content"])


def _slots(material):
    return {item["slot"]: item["text"] for item in material["runtime.short_state"]}


async def _publish_rollup(database, conversation, covered):
    now = datetime.now(UTC)
    async with database.immediate_session() as writer:
        owner = await writer.get(CanonicalConversationModel, conversation)
        writer.add(
            CanonicalConversationRollupModel(
                conversation_id=conversation,
                generation=owner.generation,
                covered_through_event_id=covered,
                summary_text="先前的大段聊天已整理，继续当前要求。",
                summary_kind="model",
                source_fingerprint="b" * 64,
                revision=1,
                created_at=now,
                updated_at=now,
            )
        )
        owner.covered_through_event_id = covered


@pytest.mark.parametrize("boundary", ["capacity", "rollup_ready"])
async def test_changed_candidate_keeps_remaining_delta_and_reintroduces_only_missing_values(
    database, tmp_path, monkeypatch, boundary
):
    provider = FakeLLMProvider()
    env, harness, chat, state, message = await _scene(database, tmp_path, provider)
    state.update({"slot": 2, "text": "retained-other-slot"})
    first = replace(message, text="已讨论的详细资料。" * 4_000)
    monkeypatch.setattr(harness.settings, "max_input_characters", len(first.text))
    assert (await harness.processor.handle(first, MemorySender())).reason == "chat"
    baseline = await _saved(database)
    first_event = json.loads(baseline.payload_json)[-1]["event_ids"][0]
    state.update({"slot": 1, "text": "delta-value-B"})
    await harness.processor.handle(
        replace(message, message_id="remaining-material-delta", text="中途明确新的记录。"),
        MemorySender(),
    )
    previous = await _saved(database)
    assert _slots(json.loads(previous.visible_material_json)) == {
        1: "delta-value-B",
        2: "retained-other-slot",
    }
    assert _changes(provider.requests[-1].messages[-1])["runtime.short_state"] == [
        {"slot": 1, "text": "delta-value-B"}
    ]
    await _publish_rollup(database, env.context.conversation_id, first_event)

    old_request = provider.requests[-1]
    first_input = next(
        entry for entry in old_request.messages if "已讨论的详细资料。" in (entry.content or "")
    )
    # The original complete request fits initially. Select a real input budget
    # between it and the published Rollup candidate; no test-only fit callback.
    smaller_budget = (
        estimate_request_tokens(old_request) - estimate_text_tokens(first_input.content) // 2
    )
    compositions = []
    compose = chat.runtime.main_turns.compose

    async def capture(**kwargs):
        if boundary == "rollup_ready":
            runtime = kwargs["runtime"]
            kwargs["runtime"] = replace(
                runtime,
                context=replace(
                    runtime.context,
                    compaction_window_tokens=math.ceil(
                        (smaller_budget + 4096) / runtime.context.compaction_trigger_ratio
                    ),
                ),
            )
        composition = await compose(**kwargs)
        compositions.append(composition)
        return composition

    monkeypatch.setattr(chat.runtime.main_turns, "compose", capture)
    if boundary == "capacity":
        monkeypatch.setattr(
            chat.runtime.runner._models,
            "capacity",
            lambda _task: ModelCapacity(input_tokens=smaller_budget),
        )
    result = await harness.processor.handle(
        replace(message, message_id="material-after-prefix-change", text="继续整理后的记录。"),
        MemorySender(),
    )
    assert result.reason == "chat"
    saved = await _saved(database)
    assert saved.epoch_id != previous.epoch_id and saved.rebuild_reason == boundary
    request = provider.requests[-1]
    assert all("已讨论的详细资料。" not in (entry.content or "") for entry in request.messages)
    remaining = [
        _changes(entry)["runtime.short_state"]
        for entry in request.messages[:-1]
        if "runtime.short_state" in _changes(entry)
    ]
    assert remaining == [[{"slot": 1, "text": "delta-value-B"}]]
    current = _changes(request.messages[-1])
    assert current["runtime.short_state"] == [{"slot": 2, "text": "retained-other-slot"}]
    material = json.loads(saved.visible_material_json)
    assert _slots(material) == {1: "delta-value-B", 2: "retained-other-slot"}
    assert material["context.people_and_scene"] == current["context.people_and_scene"]
    assert material == compositions[-1].visible_material
    assert json.loads(saved.payload_json)[-1]["message"]["content"] == request.messages[-1].content


async def test_projection_cas_failure_keeps_old_map_and_never_reaches_transport(
    database, tmp_path, monkeypatch
):
    wire = []

    async def transport(request):
        wire.append(json.loads(request.content))
        return httpx.Response(200, json=_chat_reply())

    async with httpx.AsyncClient(
        base_url="https://wire.invalid/v1/", transport=httpx.MockTransport(transport)
    ) as client:
        provider = OpenAICompatibleProvider(
            base_url="https://wire.invalid/v1",
            api_key="synthetic",
            timeout_seconds=1,
            max_retries=3,
            client=client,
        )
        _, harness, chat, state, message = await _scene(database, tmp_path, provider)
        sender = MemorySender()
        await harness.processor.handle(message, sender)
        before = await _saved(database)
        state.update({"slot": 1, "text": "uncommitted-value-B"})
        compose = chat.runtime.main_turns.compose
        stale = False

        async def conflicted(**kwargs):
            composition = await compose(**kwargs)
            commit = composition.commit_projection

            async def stale_commit():
                nonlocal stale
                if not stale:
                    stale = True
                    # Another admitted publisher advances the original CAS
                    # before this composition enters its projection writer.
                    async with database.immediate_session() as writer:
                        await writer.execute(
                            update(PromptProjectionModel)
                            .where(PromptProjectionModel.view_key == before.view_key)
                            .values(revision=PromptProjectionModel.revision + 1)
                        )
                await commit()

            return replace(composition, commit_projection=stale_commit)

        monkeypatch.setattr(chat.runtime.main_turns, "compose", conflicted)
        result = await harness.processor.handle(
            replace(message, message_id="rejected-material-cas", text="尚未派发的新资料。"),
            sender,
        )
        assert stale and result.reason != "chat"
        saved = await _saved(database)
        assert saved.visible_material_json == before.visible_material_json
        assert saved.payload_json == before.payload_json
        assert len(wire) == 1 and sender.calls == 0
        await harness.processor.handle(
            replace(message, message_id="actual-material-after-cas", text="重新发起真实下一轮。"),
            sender,
        )
        assert _changes(_wire_current(wire[-1]))["runtime.short_state"] == [
            {"slot": 1, "text": "uncommitted-value-B"}
        ]
        assert _slots(json.loads((await _saved(database)).visible_material_json)) == {
            1: "uncommitted-value-B"
        }


async def test_committed_material_survives_interrupt_before_prepared_http_dispatch(
    database, tmp_path, monkeypatch
):
    wire, prepared, committed = [], [], []

    async def transport(request):
        wire.append(json.loads(request.content))
        return httpx.Response(200, json=_chat_reply())

    async with httpx.AsyncClient(
        base_url="https://wire.invalid/v1/", transport=httpx.MockTransport(transport)
    ) as client:
        build_request = client.build_request

        def capture_prepared(*args, **kwargs):
            request = build_request(*args, **kwargs)
            prepared.append(json.loads(request.content))
            return request

        monkeypatch.setattr(client, "build_request", capture_prepared)
        provider = OpenAICompatibleProvider(
            base_url="https://wire.invalid/v1",
            api_key="synthetic",
            timeout_seconds=1,
            max_retries=3,
            client=client,
        )
        env, harness, chat, state, message = await _scene(database, tmp_path, provider)
        chat._tools.social_service = env.service
        sender = MemorySender()
        await harness.processor.handle(message, sender)
        before = await _saved(database)
        state.update({"slot": 1, "text": "committed-before-http-B"})
        compose = chat.runtime.main_turns.compose

        async def interrupted(**kwargs):
            composition = await compose(**kwargs)
            commit = composition.commit_projection

            async def commit_then_interrupt():
                await commit()
                # The executor admits before serialization; the adapter checks
                # again after preparing its real HTTP request. Interrupt at the
                # latter boundary, with the original projection already saved.
                if len(prepared) > len(wire):
                    saved = await _saved(database)
                    committed.append((saved.payload_json, saved.visible_material_json))
                    raise LLMError("synthetic interruption before HTTP dispatch")

            return replace(composition, commit_projection=commit_then_interrupt)

        monkeypatch.setattr(chat.runtime.main_turns, "compose", interrupted)
        result = await harness.processor.handle(
            replace(message, message_id="committed-before-http", text="派发前中断的新资料。"),
            sender,
        )
        assert result.reason != "chat" and len(committed) == 1
        assert len(prepared) == 2 and len(wire) == 1
        pending = _wire_current(prepared[-1])
        assert _changes(pending)["runtime.short_state"] == [
            {"slot": 1, "text": "committed-before-http-B"}
        ]
        saved = await _saved(database)
        assert saved.payload_json != before.payload_json
        assert (saved.payload_json, saved.visible_material_json) == committed[0]
        assert json.loads(saved.payload_json)[-1]["message"]["content"] == pending.content
        assert _slots(json.loads(saved.visible_material_json)) == {1: "committed-before-http-B"}
        frozen = json.loads(saved.payload_json)

        monkeypatch.setattr(chat.runtime.main_turns, "compose", compose)
        recovered = await harness.processor.handle(
            replace(message, message_id="after-undispatched-http", text="沿原资料继续下一条。"),
            sender,
        )
        assert recovered.reason == "chat" and len(wire) == 2
        assert "runtime.short_state" not in _changes(_wire_current(wire[-1]))
        assert [
            _changes(ChatMessage("user", entry["content"]))["runtime.short_state"]
            for entry in wire[-1]["messages"][:-1]
            if "runtime.short_state" in _changes(ChatMessage("user", entry["content"]))
        ].count([{"slot": 1, "text": "committed-before-http-B"}]) == 1
        after = await _saved(database)
        assert json.loads(after.payload_json)[: len(frozen)] == frozen
        assert after.visible_material_json == saved.visible_material_json
        assert sender.calls == 0
        assert not any(action == "send_group_msg" for action, _ in env.bot.calls)


async def test_successful_dispatch_without_send_still_commits_the_actual_material(
    database, tmp_path
):
    observed = []

    async def transport(request):
        body = json.loads(request.content)
        # This is the actual adapter transport boundary, after Main admission.
        saved = await _saved(database)
        observed.append((body, saved.payload_json, saved.visible_material_json))
        return httpx.Response(200, json=_chat_reply())

    async with httpx.AsyncClient(
        base_url="https://wire.invalid/v1/", transport=httpx.MockTransport(transport)
    ) as client:
        provider = OpenAICompatibleProvider(
            base_url="https://wire.invalid/v1",
            api_key="synthetic",
            timeout_seconds=1,
            max_retries=3,
            client=client,
        )
        env, harness, chat, state, message = await _scene(database, tmp_path, provider)
        chat._tools.social_service = env.service
        sender = MemorySender()
        await harness.processor.handle(message, sender)
        state.update({"slot": 1, "text": "model-observed-value-B"})
        result = await harness.processor.handle(
            replace(message, message_id="observed-without-send", text="只内部处理这条。"), sender
        )
        assert result.reason == "chat" and sender.calls == 0
        body, frozen, material = observed[-1]
        current = _wire_current(body)
        assert _changes(current)["runtime.short_state"] == [
            {"slot": 1, "text": "model-observed-value-B"}
        ]
        assert json.loads(frozen)[-1]["message"]["content"] == current.content
        assert _slots(json.loads(material)) == {1: "model-observed-value-B"}
        saved = await _saved(database)
        assert (saved.payload_json, saved.visible_material_json) == (frozen, material)


async def test_http_failure_preserves_attempted_material_and_does_not_repeat_confirmed_send(
    database, tmp_path
):
    attempts = []
    sent_text = "只发送这一次已确认的结果。"

    async def transport(request):
        body = json.loads(request.content)
        saved = await _saved(database)
        attempts.append((body, saved.payload_json, saved.visible_material_json))
        if len(attempts) == 3:
            return httpx.Response(401, json={"error": {"message": "synthetic rejection"}})
        return httpx.Response(
            200, json=_chat_reply(send_text=sent_text if len(attempts) == 2 else None)
        )

    async with httpx.AsyncClient(
        base_url="https://wire.invalid/v1/", transport=httpx.MockTransport(transport)
    ) as client:
        provider = OpenAICompatibleProvider(
            base_url="https://wire.invalid/v1",
            api_key="synthetic",
            timeout_seconds=1,
            max_retries=3,
            client=client,
        )
        env, harness, chat, state, message = await _scene(database, tmp_path, provider)
        chat._tools.social_service = env.service
        sender = MemorySender()
        await harness.processor.handle(message, sender)
        state.update({"slot": 1, "text": "attempted-http-value-B"})
        changed = replace(message, message_id="http-attempted-material", text="处理并发一次结果。")
        result = await harness.processor.handle(changed, sender)
        assert result.reason != "chat" and len(attempts) == 3
        assert sum(action == "send_group_msg" for action, _ in env.bot.calls) == 1
        assert not sender.messages
        body, frozen, material = attempts[1]
        current = _wire_current(body)
        assert _changes(current)["runtime.short_state"] == [
            {"slot": 1, "text": "attempted-http-value-B"}
        ]
        assert json.loads(frozen)[-1]["message"]["content"] == current.content
        attempted_frozen, attempted_material = attempts[2][1:]
        original_items = json.loads(frozen)
        assert json.loads(attempted_frozen)[: len(original_items)] == original_items
        assert attempted_material == material
        assert any(
            '"succeeded"' in (item.get("content") or "")
            for item in attempts[2][0]["messages"]
            if item["role"] == "tool"
        )
        assert _slots(json.loads(material)) == {1: "attempted-http-value-B"}
        saved = await _saved(database)
        assert (saved.payload_json, saved.visible_material_json) == (
            attempted_frozen,
            attempted_material,
        )
        async with database.sessions() as reader:
            delivered = list(
                await reader.scalars(
                    select(ChatEventModel).where(
                        ChatEventModel.direction == "outbound", ChatEventModel.content == sent_text
                    )
                )
            )
        assert len(delivered) == 1 and delivered[0].platform_message_id
        await harness.processor.handle(changed, sender)
        assert len(attempts) == 3
        assert sum(action == "send_group_msg" for action, _ in env.bot.calls) == 1
        assert (await _saved(database)).visible_material_json == material
