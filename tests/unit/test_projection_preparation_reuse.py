"""Reused prefix preparation remains frozen and subordinate to publication CAS."""

import json
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import replace

import pytest
from sqlalchemy import event, select, text
from tests.unit.test_projection_selection_delta import chat, scene

from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel
from qq_ai_bot.conversation.observation_models import ContextSelectionModel
from qq_ai_bot.conversation.projections import ProjectionConflict


async def previous(database, tmp_path):
    _, repository, arguments = await scene(database, tmp_path)
    old = await repository.commit(
        **arguments, items=list(chat(20).items), rebuild_reason="bootstrap"
    )
    return repository, arguments, old


def options(arguments, old):
    return dict(
        **arguments,
        expected_epoch=old.epoch_id,
        expected_revision=old.revision,
        previous_snapshot=old,
        previous_item_count=20,
    )


async def test_prefix_reuses_prepared_items_and_snapshot_without_body_reread(
    database, tmp_path, monkeypatch
):
    repository, arguments, old = await previous(database, tmp_path)
    items = list(chat(21).items)
    expected = deepcopy(items)
    statements, decoded = [], []
    load = json.loads

    def loads(value, *args, **kwargs):
        if value == old.payload_json:
            decoded.append(value)
        return load(value, *args, **kwargs)

    def capture(_conn, _cursor, statement, *_args):
        statements.append(statement)

    monkeypatch.setattr(json, "loads", loads)
    event.listen(database.engine.sync_engine, "before_cursor_execute", capture)
    try:
        plan = await repository.prepare_commit(**options(arguments, old), items=items)
    finally:
        event.remove(database.engine.sync_engine, "before_cursor_execute", capture)
    assert not decoded
    assert not any("prompt_projections.payload_json" in sql for sql in statements)
    items[0]["message"]["content"] = "mutated caller"
    async with database.immediate_session() as writer:
        saved = await plan(writer)
    assert saved.items() == expected
    assert old.items() == list(chat(20).items)
    assert (
        saved.payload_json.encode()
        == json.dumps(expected, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
    )


async def test_reused_prefix_fails_closed_if_projection_changes_before_publish(database, tmp_path):
    repository, arguments, old = await previous(database, tmp_path)
    plan = await repository.prepare_commit(**options(arguments, old), items=list(chat(21).items))
    rival = await repository.commit(
        **arguments,
        items=list(chat(22).items),
        expected_epoch=old.epoch_id,
        expected_revision=old.revision,
    )
    with pytest.raises(ProjectionConflict):
        async with database.immediate_session() as writer:
            await plan(writer)
    assert (await repository.read(arguments["view_key"])).payload_json == rival.payload_json
    async with database.sessions() as reader:
        assert len((await reader.scalars(select(ContextSelectionModel.id))).all()) == 22


@pytest.mark.parametrize("change", ["payload", "view"])
async def test_repository_snapshot_cannot_substitute_prefix_or_view(database, tmp_path, change):
    repository, arguments, old = await previous(database, tmp_path)
    items = list(chat(21).items)
    if change == "payload":
        items[0]["message"]["content"] = "rewritten old history"
        offered = replace(
            old,
            payload_json=json.dumps(
                items[:20], ensure_ascii=False, separators=(",", ":"), allow_nan=False
            ),
        )
    else:
        offered = old
        arguments = dict(arguments, view_key="d" * 64)
    statements = []

    def capture(_conn, _cursor, statement, *_args):
        statements.append(statement)

    event.listen(database.engine.sync_engine, "before_cursor_execute", capture)
    try:
        with pytest.raises(ProjectionConflict, match="snapshot origin changed"):
            await repository.prepare_commit(**options(arguments, offered), items=items)
    finally:
        event.remove(database.engine.sync_engine, "before_cursor_execute", capture)
    assert not any("prompt_projections.payload_json" in sql for sql in statements)
    assert (await repository.read("a" * 64)).payload_json == old.payload_json
    assert await repository.read("d" * 64) is None
    async with database.sessions() as reader:
        assert len((await reader.scalars(select(ContextSelectionModel.id))).all()) == 20


@pytest.mark.parametrize("change", ["source", "generation", "privacy"])
async def test_reused_prefix_prepare_publish_fences_remain(database, tmp_path, change):
    repository, arguments, old = await previous(database, tmp_path)
    plan = await repository.prepare_commit(**options(arguments, old), items=list(chat(21).items))
    async with database.immediate_session() as writer:
        if change == "privacy":
            await writer.execute(
                text("INSERT INTO execution_trace_state(id,privacy_generation) VALUES(1,1)")
            )
        else:
            owner = await writer.get(CanonicalConversationModel, arguments["conversation_id"])
            if change == "source":
                owner.prompt_source_revision += 1
            else:
                owner.generation += 1
    async with database.sessions() as reader:
        before = (await reader.scalars(select(ContextSelectionModel.id))).all()
    with pytest.raises(ProjectionConflict):
        async with database.immediate_session() as writer:
            await plan(writer)
    async with database.sessions() as reader:
        assert (await reader.scalars(select(ContextSelectionModel.id))).all() == before


@pytest.mark.parametrize("failure", ["rollback", "commit_ack_lost"])
async def test_prefix_publication_rollback_or_unknown_commit_no_blind_replay(
    database, tmp_path, failure
):
    repository, arguments, old = await previous(database, tmp_path)
    plan = await repository.prepare_commit(**options(arguments, old), items=list(chat(21).items))
    original = database.immediate_session

    @asynccontextmanager
    async def lost_ack():
        async with original() as writer:
            yield writer
        raise OSError("commit acknowledgement lost")

    if failure == "rollback":
        with pytest.raises(RuntimeError):
            async with original() as writer:
                await plan(writer)
                raise RuntimeError("journal failed before commit")
        assert (await repository.read(arguments["view_key"])).payload_json == old.payload_json
        expected_count = 20
    else:
        with pytest.raises(OSError):
            async with lost_ack() as writer:
                await plan(writer)
        actual = await repository.read(arguments["view_key"])
        assert actual.items() == list(chat(21).items)
        # An explicitly attempted stale publication is refused; the repository
        # does not internally repeat it or classify committed facts as absent.
        with pytest.raises(ProjectionConflict):
            async with original() as writer:
                await plan(writer)
        expected_count = 21
    async with database.sessions() as reader:
        assert len((await reader.scalars(select(ContextSelectionModel.id))).all()) == expected_count
