"""Actual Main preparations retain useful changes without replaying runtime packages."""

import json
from dataclasses import replace

import pytest
from sqlalchemy import select
from tests.conftest import MemorySender
from tests.unit.test_history_dispatch_ownership import _scene

from qq_ai_bot.conversation.observation_models import ContextObservationModel
from qq_ai_bot.conversation.projection_models import PromptProjectionModel
from qq_ai_bot.domain.messages import ChatMessage
from qq_ai_bot.llm.fake import FakeLLMProvider
from qq_ai_bot.prompting.serializer import DYNAMIC_ENVELOPE_HEADER
from qq_ai_bot.services.turn_transcript import TranscriptRequest, validating_request


def _changes(message):
    content = message.content or ""
    if not content.startswith(DYNAMIC_ENVELOPE_HEADER):
        return {}
    items, _ = json.JSONDecoder().raw_decode(content[len(DYNAMIC_ENVELOPE_HEADER) :])
    return {item["id"]: item["data"] for item in items}


async def _saved(database):
    async with database.sessions() as reader:
        return (await reader.scalars(select(PromptProjectionModel))).one()


async def test_unchanged_material_keeps_prefix_and_creates_no_new_snapshot(database, tmp_path):
    provider = FakeLLMProvider()
    _, harness, _chat, _state, message = await _scene(database, tmp_path, provider)
    await harness.processor.handle(message, MemorySender())
    first = await _saved(database)
    original = json.loads(first.payload_json)
    async with database.sessions() as reader:
        sources = list(await reader.scalars(select(ContextObservationModel.id)))
    await harness.processor.handle(
        replace(message, message_id="delta-next", text="下一条"), MemorySender()
    )
    saved = await _saved(database)
    assert saved.epoch_id == first.epoch_id
    assert json.loads(saved.payload_json)[: len(original)] == original
    assert saved.payload_json.count("original-snapshot") == 1
    assert _changes(provider.requests[-1].messages[-1]) == {}
    assert "[Observation; derived notes]" not in saved.payload_json
    async with database.sessions() as reader:
        assert list(await reader.scalars(select(ContextObservationModel.id))) == sources


async def test_slot_changes_back_and_clear_are_real_single_changes(database, tmp_path):
    provider = FakeLLMProvider()
    _, harness, _chat, state, message = await _scene(database, tmp_path, provider)
    state.update({"slot": 2, "text": "unchanged-other-slot"})
    await harness.processor.handle(message, MemorySender())
    for number, value in enumerate(("value-B", "original-snapshot", "", ""), 1):
        state.update({"slot": 1, "text": value})
        await harness.processor.handle(
            replace(message, message_id=f"delta-change-{number}", text=f"继续{number}"),
            MemorySender(),
        )
        changes = _changes(provider.requests[-1].messages[-1])
        if number == 4:
            assert "runtime.short_state" not in changes
        else:
            assert changes["runtime.short_state"] == [{"slot": 1, "text": value}]
    material = json.loads((await _saved(database)).visible_material_json)
    assert material["runtime.short_state"] == [{"slot": 2, "text": "unchanged-other-slot"}]


async def test_expiry_without_revision_change_emits_one_clear(database, tmp_path, monkeypatch):
    provider = FakeLLMProvider()
    _, harness, _chat, state, message = await _scene(database, tmp_path, provider)
    await harness.processor.handle(message, MemorySender())
    original = state.snapshot()[0]
    monkeypatch.setattr(
        "qq_ai_bot.workspace.short_state.time.time", lambda: original["expires_at"] + 1
    )
    assert state.snapshot()[0]["revision"] == original["revision"]
    for number in (1, 2):
        await harness.processor.handle(
            replace(message, message_id=f"expired-{number}", text="过期后继续"), MemorySender()
        )
        changes = _changes(provider.requests[-1].messages[-1])
        if number == 1:
            assert changes["runtime.short_state"] == [{"slot": 1, "text": ""}]
        else:
            assert "runtime.short_state" not in changes


async def test_chat_boundary_second_writer_retains_material_map(database, tmp_path):
    provider = FakeLLMProvider()
    _env, harness, chat, _state, message = await _scene(database, tmp_path, provider)
    captured = []
    original_compose = chat.runtime.main_turns.compose

    async def capture(**kwargs):
        composition = await original_compose(**kwargs)
        captured.append(composition)
        return composition

    chat.runtime.main_turns.compose = capture
    await harness.processor.handle(message, MemorySender())
    before = await _saved(database)
    observed = await harness.processor.handle(
        replace(
            message, message_id="boundary-observation", text="旁观的新聊天", mentions_bot=False
        ),
        MemorySender(),
    )
    assert observed.reason == "group_observed"
    boundary = await captured[0].observation_boundary(frozenset())
    prepared = await boundary.prepare()
    async with database.immediate_session() as writer:
        await prepared.publication(writer)
    prepared.finalize()
    after = await _saved(database)
    assert after.epoch_id == before.epoch_id
    assert after.visible_material_json == before.visible_material_json
    assert "旁观的新聊天" in after.payload_json


@pytest.mark.parametrize("evicted", [False, True])
async def test_same_trigger_new_attempt_survives_cold_restore(database, tmp_path, evicted):
    provider = FakeLLMProvider()
    _, harness, chat, state, message = await _scene(database, tmp_path, provider)
    captured = []
    original_compose = chat.runtime.main_turns.compose

    async def capture(**kwargs):
        captured.append(kwargs)
        return await original_compose(**kwargs)

    chat.runtime.main_turns.compose = capture
    await harness.processor.handle(message, MemorySender())
    state.update({"slot": 1, "text": "new-attempt-B"})
    composition = await original_compose(**captured[0])
    with validating_request(TranscriptRequest(composition.messages, None, ())):
        await composition.commit_projection()
    saved = await _saved(database)
    if evicted:
        await chat.runtime.main_turns._projections.invalidate_view(
            saved.view_key, reason="capacity"
        )
    await database.close()
    await harness.processor.handle(
        replace(message, message_id="after-attempt", text="继续同一轮结果"), MemorySender()
    )
    saved = await _saved(database)
    assert "new-attempt-B" in saved.payload_json
    assert "original-snapshot" not in saved.payload_json
    assert _changes(provider.requests[-1].messages[-1]) == {}
    assert (
        json.loads(saved.visible_material_json)["runtime.short_state"][0]["text"] == "new-attempt-B"
    )


async def test_eventless_a_b_a_retains_occurrence_order_after_eviction(database, tmp_path):
    provider = FakeLLMProvider()
    _, harness, chat, state, message = await _scene(database, tmp_path, provider)
    captured = []
    original_compose = chat.runtime.main_turns.compose

    async def capture(**kwargs):
        captured.append(kwargs)
        return await original_compose(**kwargs)

    chat.runtime.main_turns.compose = capture
    await harness.processor.handle(message, MemorySender())
    kwargs = dict(captured[0])
    kwargs["context"] = replace(
        kwargs["context"],
        current_event_id=None,
        projection_scope="self_initiative",
        current_message=ChatMessage("user", "eventless observation"),
    )
    for value in ("eventless-A", "eventless-B", "eventless-A"):
        state.update({"slot": 1, "text": value})
        composition = await original_compose(**kwargs)
        with validating_request(TranscriptRequest(composition.messages, None, ())):
            await composition.commit_projection()
    saved = await _saved(database)
    await chat.runtime.main_turns._projections.invalidate_view(saved.view_key, reason="capacity")
    composition = await original_compose(**kwargs)
    occurrences = [
        changes["runtime.short_state"][0]["text"]
        for entry in composition.messages[:-1]
        if (changes := _changes(entry)).get("runtime.short_state")
    ]
    assert occurrences[-3:] == ["eventless-A", "eventless-B", "eventless-A"]
    assert "runtime.short_state" not in _changes(composition.messages[-1])
