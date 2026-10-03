"""Exact existing clues are read-only; publication still checks live sources."""

import json
from contextlib import asynccontextmanager
from dataclasses import replace

import pytest
from sqlalchemy import event, select, update
from tests.support.social_identity_cases import social_env
from tests.unit.test_context_note_artifact_lifecycle import admitted, note
from tests.unit.test_context_observation_sources import add_clue, context_for, prepare, summary

from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel
from qq_ai_bot.conversation.observation_models import ContextObservationModel
from qq_ai_bot.conversation.observations import ContextObservationRepository
from qq_ai_bot.conversation.projections import ProjectionConflict
from qq_ai_bot.domain.messages import ChatMessage
from qq_ai_bot.execution_trace.db_models import ExecutionTraceStateModel


@asynccontextmanager
async def held_writer(database):
    statements = []

    def capture(_conn, _cursor, statement, *_args):
        statements.append(statement)

    async with database.immediate_session():
        event.listen(database.engine.sync_engine, "before_cursor_execute", capture)
        try:
            yield statements
        finally:
            event.remove(database.engine.sync_engine, "before_cursor_execute", capture)
    assert not any(
        sql.lstrip().upper().startswith(("BEGIN IMMEDIATE", "INSERT", "UPDATE", "DELETE"))
        for sql in statements
    )


async def test_duplicate_note_reads_original_scope_while_another_connection_holds_writer(
    database, tmp_path
):
    control, _access = await admitted(database, tmp_path)
    await control.execute("task_control", {"action": "update", "context_note": note()}, "note-1")
    saved = await control.repository.get(control.current["id"])
    payload = json.loads(saved["checkpoint_json"])["context_note"]["payload"]
    repository = ContextObservationRepository(database)
    async with database.sessions() as reader:
        expected = await reader.scalar(select(ContextObservationModel.id))
    async with held_writer(database):
        assert await repository.publish_note(saved, 1, payload) == expected
    async with database.immediate_session() as writer:
        writer.add(ExecutionTraceStateModel(id=1, privacy_generation=1))
    with pytest.raises(ProjectionConflict, match="privacy changed"):
        await repository.publish_note(saved, 1, payload)


async def test_duplicate_prepared_snapshot_never_publishes_selection(database, tmp_path):
    env = await social_env(database, tmp_path)
    base = await context_for(database)
    identity = next(iter(base.visible_event_ids))
    context = replace(
        base,
        current_event_id=identity,
        history_messages=(),
        history_fragments=(),
        history_event_fragments=(),
    )
    prepared = await prepare(database, env, context, None)
    compiled = prepared.fragments.append_current(identity, ChatMessage("user", "current"))
    payload = {"context": {"fixed": "snapshot"}}
    first = await prepared.prepare_commit(compiled, current_snapshot=payload)
    async with held_writer(database):
        second = await prepared.prepare_commit(compiled, current_snapshot=payload)
        assert second.observation_sources == first.observation_sources
    from qq_ai_bot.conversation.observation_models import ContextSelectionModel

    async with database.sessions() as reader:
        assert await reader.scalar(select(ContextSelectionModel.id)) is None


async def summary_case(database, tmp_path):
    env = await social_env(database, tmp_path)
    await add_clue(database, env, "parent-a")
    repository = ContextObservationRepository(database)
    rows = await repository.read(
        conversation_id=env.context.conversation_id,
        generation=1,
        actor_id=env.person,
        read_scope="main",
    )
    async with database.sessions() as reader:
        owner = await reader.get(CanonicalConversationModel, env.context.conversation_id)
        revision = owner.prompt_source_revision
    arguments = dict(
        view_key="a" * 64,
        observations=rows,
        payload=summary(rows),
        conversation_id=env.context.conversation_id,
        generation=1,
        actor_id=env.person,
        read_scope="main",
        expected_source_revision=revision,
    )
    return repository, arguments


async def test_scope_summary_prepares_parents_before_writer_and_duplicate_is_read_only(
    database, tmp_path
):
    repository, arguments = await summary_case(database, tmp_path)
    statements = []

    def capture(_conn, _cursor, statement, *_args):
        statements.append(statement)

    event.listen(database.engine.sync_engine, "before_cursor_execute", capture)
    try:
        first = await repository.publish_scope_summary(**arguments)
    finally:
        event.remove(database.engine.sync_engine, "before_cursor_execute", capture)
    boundary = statements.index("BEGIN IMMEDIATE")
    traversals = [
        i
        for i, sql in enumerate(statements)
        if sql.startswith(
            "SELECT model_context_observations.id, model_context_observations.version,"
        )
    ]
    assert traversals and all(i < boundary for i in traversals)
    async with held_writer(database):
        assert await repository.publish_scope_summary(**arguments) == first


@pytest.mark.parametrize("change", ["parent", "privacy"])
async def test_summary_rechecks_snapshot_before_publication(
    database, tmp_path, monkeypatch, change
):
    repository, arguments = await summary_case(database, tmp_path)
    original = database.immediate_session
    entered = False

    @asynccontextmanager
    async def changed_writer():
        nonlocal entered
        if not entered:
            entered = True
            async with original() as writer:
                if change == "parent":
                    await writer.execute(
                        update(ContextObservationModel)
                        .where(ContextObservationModel.id == "parent-a")
                        .values(payload_json='{"changed":true}')
                    )
                else:
                    writer.add(ExecutionTraceStateModel(id=1, privacy_generation=1))
        async with original() as writer:
            yield writer

    monkeypatch.setattr(database, "immediate_session", changed_writer)
    with pytest.raises(ProjectionConflict, match="source changed"):
        await repository.publish_scope_summary(**arguments)
    async with database.sessions() as reader:
        assert (
            await reader.scalar(
                select(ContextObservationModel.id).where(
                    ContextObservationModel.summary_view_key == arguments["view_key"]
                )
            )
            is None
        )
