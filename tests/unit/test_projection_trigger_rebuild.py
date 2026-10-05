"""Representation recovery never installs the new trigger twice."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import delete, select
from tests.unit.test_projection_selection_delta import scene

from qq_ai_bot.conversation.frozen_fragments import FrozenFragments
from qq_ai_bot.conversation.observation_models import ContextSelectionModel
from qq_ai_bot.conversation.projection_models import PromptProjectionModel
from qq_ai_bot.domain.conversations import ConversationScope
from qq_ai_bot.domain.messages import ChatMessage
from qq_ai_bot.persistence.event_repository import ConversationReadVersion
from qq_ai_bot.services.context_assembler import AssembledContext, ContextMetrics
from qq_ai_bot.services.history_projection import prepare_history
from qq_ai_bot.time.models import TimeContext


@pytest.mark.parametrize(
    "boundary", ["contract_changed", "bootstrap", "capacity", "invalidated_capacity"]
)
@pytest.mark.parametrize("grouped", [False, True], ids=["single-trigger", "grouped-trigger"])
async def test_rebuild_selection_excludes_current_trigger_without_rewriting_history(
    database, tmp_path, boundary, grouped
):
    env, repository, arguments = await scene(database, tmp_path)
    exact = ChatMessage("user", "[exact frozen dynamic snapshot]\nold name: preserved history")
    old = FrozenFragments.load([]).append_current(10, exact)
    old = old.extend_history(
        (((20, 30) if grouped else (30,), ChatMessage("user", "old trigger envelope")),), ()
    )
    if boundary == "capacity":
        old = old.append_current(40, ChatMessage("user", "outside current read window"))
    previous = await repository.commit(
        **arguments, items=list(old.items), rebuild_reason="bootstrap"
    )
    if boundary == "bootstrap":
        # Cache eviction drops the projection, retaining original selections.
        async with database.immediate_session() as session:
            await session.execute(
                delete(PromptProjectionModel).where(
                    PromptProjectionModel.view_key == arguments["view_key"]
                )
            )
    elif boundary == "invalidated_capacity":
        await repository.invalidate_view(arguments["view_key"], reason="capacity")
    async with database.sessions() as session:
        original_selections = list(
            (
                await session.execute(
                    select(ContextSelectionModel.id, ContextSelectionModel.payload_json)
                    .where(ContextSelectionModel.view_key == arguments["view_key"])
                    .order_by(ContextSelectionModel.id)
                )
            ).all()
        )
    now = datetime.now(UTC)
    context = AssembledContext(
        metadata_payload={},
        history_messages=(ChatMessage("user", "new render ten and twenty"),),
        current_message=ChatMessage("user", "new current trigger envelope"),
        recent_delivery=(),
        current_time=TimeContext(now, now, "UTC"),
        current_relationship=None,
        metrics=ContextMetrics(0, 0, 1, 0, False),
        visible_event_ids=frozenset({10, 20, 30}),
        read_version=ConversationReadVersion(
            ConversationScope.group(env.bot.self_id, "20001"),
            arguments["conversation_id"],
            arguments["generation"],
            arguments["starts_after_event_id"],
            arguments["expected_source_revision"],
        ),
        history_fragments=(((10, 20), ChatMessage("user", "new render ten and twenty")),),
        history_event_fragments=(
            ((10,), ChatMessage("user", "new render ten")),
            ((20,), ChatMessage("user", "new render twenty")),
        ),
        current_event_id=30,
    )
    prepared = await prepare_history(
        repository,
        context,
        view_key=arguments["view_key"],
        context_key=arguments["context_key"],
        contract_revision="d" * 64
        if boundary == "contract_changed"
        else arguments["contract_revision"],
        actor_id=arguments["actor_id"],
        read_scope=arguments["read_scope"],
        history_fits=lambda _messages: True,
    )
    assert prepared.reason == (
        "capacity" if boundary == "invalidated_capacity" else "source_changed"
    )
    assert prepared.fragments.event_ids == {10, 20}
    assert prepared.fragments.items[0] == old.items[0]
    assert prepared.fragments.messages()[0] == exact
    assert prepared.fragments.messages()[1].content == "new render twenty"
    submitted = prepared.fragments.append_current(30, context.current_message)
    assert submitted.event_ids == {10, 20, 30}
    assert sum(30 in item["event_ids"] for item in submitted.items) == 1
    committed = await prepared.commit(submitted)
    assert committed.epoch_id != previous.epoch_id
    assert previous.items() == list(old.items)
    async with database.sessions() as session:
        selections = dict(
            (
                await session.execute(
                    select(ContextSelectionModel.id, ContextSelectionModel.payload_json)
                    .where(ContextSelectionModel.view_key == arguments["view_key"])
                    .order_by(ContextSelectionModel.id)
                )
            ).all()
        )
    assert all(selections[identity] == payload for identity, payload in original_selections)


async def test_actorless_work_resume_keeps_selected_history_trigger_without_new_current(
    database, tmp_path
):
    env, repository, arguments = await scene(database, tmp_path)
    old = FrozenFragments.load([]).append_current(30, ChatMessage("user", "exact old trigger"))
    await repository.commit(**arguments, items=list(old.items), rebuild_reason="bootstrap")
    now = datetime.now(UTC)
    context = AssembledContext(
        metadata_payload={},
        history_messages=(ChatMessage("user", "new history render"),),
        current_message=ChatMessage("user", "Work resume control envelope"),
        recent_delivery=(),
        current_time=TimeContext(now, now, "UTC"),
        current_relationship=None,
        metrics=ContextMetrics(0, 0, 1, 0, False),
        visible_event_ids=frozenset({30}),
        read_version=ConversationReadVersion(
            ConversationScope.group(env.bot.self_id, "20001"),
            arguments["conversation_id"],
            arguments["generation"],
            arguments["starts_after_event_id"],
            arguments["expected_source_revision"],
        ),
        history_fragments=(((30,), ChatMessage("user", "new history render")),),
        history_event_fragments=(((30,), ChatMessage("user", "new history render")),),
        current_event_id=None,
        projection_scope="main",
    )
    prepared = await prepare_history(
        repository,
        context,
        view_key=arguments["view_key"],
        context_key=arguments["context_key"],
        contract_revision="d" * 64,
        actor_id=arguments["actor_id"],
        read_scope=arguments["read_scope"],
        history_fits=lambda _messages: True,
    )
    assert prepared.reason == "contract_changed"
    assert prepared.fragments.items == old.items
