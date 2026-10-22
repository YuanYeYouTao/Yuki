"""Real SQLite append work follows new chunks, with the original publication fence."""

import json
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from sqlalchemy import event, insert, select, update
from tests.unit.test_history_missing_body import scene

from yuki.conversation.frozen_fragments import FrozenFragments
from yuki.conversation.observation_models import ContextSelectionModel
from yuki.conversation.projection_models import PromptProjectionChunkModel
from yuki.conversation.projections import ProjectionConflict, PromptProjectionRepository
from yuki.conversation.prompt_history import PromptHistorySource
from yuki.domain.messages import ChatMessage
from yuki.persistence.models import ChatEventModel


async def _prepare(repository, context, fragments, previous=None, *, reason=None):
    version = context.read_version
    return await repository.prepare_commit(
        view_key=version.conversation_id,
        conversation_id=version.conversation_id,
        generation=version.generation,
        expected_source_revision=version.prompt_source_revision,
        starts_after_event_id=version.starts_after_event_id,
        context_key="real-chunk-cost",
        fragments=fragments,
        expected_epoch=previous.epoch_id if previous else None,
        expected_revision=previous.revision if previous else 0,
        previous_snapshot=previous,
        rebuild_reason=reason,
    )


async def _chunks(database):
    async with database.read_session() as reader:
        return list(
            await reader.execute(
                select(
                    PromptProjectionChunkModel.epoch_id,
                    PromptProjectionChunkModel.ordinal,
                    PromptProjectionChunkModel.payload_json,
                ).order_by(PromptProjectionChunkModel.ordinal)
            )
        )


async def test_hot_append_does_not_materialize_or_rewrite_the_original_prefix(
    database, monkeypatch
):
    _ledger, context, _seed = await scene(database, 512)
    repository = PromptProjectionRepository(database)
    base = FrozenFragments.load([]).extend_history(
        context.history_fragments[:-1], context.history_event_fragments
    )
    publish = await _prepare(repository, context, base, reason="bootstrap")
    async with database.immediate_session() as writer:
        await publish(writer)
    previous = await repository.read(context.read_version.conversation_id)
    original = await _chunks(database)
    candidate = previous.fragments.extend_history(
        context.history_fragments[-1:], context.history_event_fragments[-1:]
    )
    writes = []

    def record(_conn, _cursor, statement, params, *_):
        if statement.startswith(
            ("INSERT INTO prompt_projection_chunks", "UPDATE prompt_projections")
        ):
            writes.append((statement, params))

    def no_full_export(_self):
        raise AssertionError("hot append exported the entire old prefix")

    monkeypatch.setattr(FrozenFragments, "items", property(no_full_export))
    event.listen(database.engine.sync_engine, "before_cursor_execute", record)
    try:
        publish = await _prepare(repository, context, candidate, previous)
        async with database.immediate_session() as writer:
            committed = await publish(writer)
    finally:
        event.remove(database.engine.sync_engine, "before_cursor_execute", record)
    after = await _chunks(database)
    assert after[: len(original)] == original
    assert len(after) == len(original) + 1
    assert len(after[-1].payload_json.encode()) < len(original[0].payload_json.encode()) / 10
    assert all("payload_json" not in sql for sql, _ in writes if sql.startswith("UPDATE"))
    assert committed.fragments.delta_from(previous.fragments)
    assert len(previous.fragments) == len(base)
    assert tuple(committed.fragments.iter_fragments()) == tuple(candidate.iter_fragments())


async def test_unchanged_candidate_does_not_publish_a_new_head_or_chunk(database):
    _ledger, context, _seed = await scene(database, 4)
    repository = PromptProjectionRepository(database)
    initial = FrozenFragments.load([]).extend_history(context.history_fragments, ())
    publish = await _prepare(repository, context, initial, reason="bootstrap")
    async with database.immediate_session() as writer:
        await publish(writer)
    previous = await repository.read(context.read_version.conversation_id)
    original = await _chunks(database)
    publish = await _prepare(repository, context, previous.fragments, previous)
    async with database.immediate_session() as writer:
        committed = await publish(writer)
    assert committed is previous
    assert await _chunks(database) == original


async def test_stale_candidate_and_forged_prefix_cannot_publish_or_mutate_a_snapshot(database):
    _ledger, context, _seed = await scene(database, 5)
    repository = PromptProjectionRepository(database)
    base = FrozenFragments.load([]).extend_history(context.history_fragments[:1], ())
    publish = await _prepare(repository, context, base, reason="bootstrap")
    async with database.immediate_session() as writer:
        await publish(writer)
    previous = await repository.read(context.read_version.conversation_id)
    first = previous.fragments.extend_history(context.history_fragments[1:2], ())
    later = previous.fragments.extend_history(context.history_fragments[2:3], ())
    publish_first = await _prepare(repository, context, first, previous)
    publish_later = await _prepare(repository, context, later, previous)
    async with database.immediate_session() as writer:
        await publish_first(writer)
    original = await _chunks(database)
    with pytest.raises(ProjectionConflict):
        async with database.immediate_session() as writer:
            await publish_later(writer)
    assert await _chunks(database) == original
    assert len(previous.fragments) == 1
    forged = replace(previous, fragments=later)
    with pytest.raises(ProjectionConflict):
        await _prepare(repository, context, later, forged)


async def test_replacement_stores_current_bytes_and_retires_old_chunks_not_provenance(database):
    _ledger, context, _seed = await scene(database, 3)
    repository = PromptProjectionRepository(database)
    identity = context.history_fragments[0][0][0]
    original = FrozenFragments.load([]).append_current(
        identity, ChatMessage("user", "first admitted")
    )
    publish = await _prepare(repository, context, original, reason="bootstrap")
    async with database.immediate_session() as writer:
        await publish(writer)
    previous = await repository.read(context.read_version.conversation_id)
    replacement = FrozenFragments.load([]).append_current(
        identity, ChatMessage("user", "current epoch")
    )
    publish = await _prepare(repository, context, replacement, previous, reason="compact")
    async with database.immediate_session() as writer:
        current = await publish(writer)
    assert current.epoch_id != previous.epoch_id
    assert [(epoch, ordinal) for epoch, ordinal, _ in await _chunks(database)] == [
        (current.epoch_id, 1)
    ]
    async with database.read_session() as reader:
        first = await reader.scalar(select(ContextSelectionModel.payload_json))
    assert "first admitted" in first and "current epoch" not in first
    assert current.items()[0]["message"]["content"] == "current epoch"


def _source(projection=None):
    return PromptHistorySource(
        (), projection=projection, timezone="UTC", after_event_id=0
    )


def test_source_metadata_uses_the_same_total_memory_budget_and_lru(database):
    first, second = _source(), _source()
    repository = PromptProjectionRepository(
        database, total_bytes=first.estimated_bytes + second.estimated_bytes - 1
    )
    repository.remember_history_source("first", first)
    repository.remember_history_source("second", second)
    assert repository.history_source("first") is None
    assert repository.history_source("second") is second


async def test_unpublished_source_remains_reusable_without_admitting_a_projection(database):
    source = _source()
    repository = PromptProjectionRepository(database)
    repository.remember_history_source("unpublished", source)
    assert await repository.read("unpublished") is None
    assert repository.history_source("unpublished") is source


async def test_oversized_derived_source_does_not_discard_the_public_projection(database):
    _ledger, context, _seed = await scene(database, 2)
    source = _source()
    repository = PromptProjectionRepository(database, max_view_bytes=source.estimated_bytes - 1)
    identity = context.history_fragments[0][0][0]
    fragments = FrozenFragments.load([]).append_current(identity, ChatMessage("user", "public"))
    publish = await _prepare(repository, context, fragments, reason="bootstrap")
    async with database.immediate_session() as writer:
        await publish(writer)
    key = context.read_version.conversation_id
    original = await repository.read(key)
    repository.remember_history_source(key, source)
    assert repository.history_source(key) is None
    assert await repository.read(key) is original
    assert original.items()[0]["message"]["content"] == "public"


async def test_real_source_invalidation_does_not_leave_old_public_body_pinned_by_source(database):
    _ledger, context, _seed = await scene(database, 2)
    repository = PromptProjectionRepository(database)
    identity = context.history_fragments[0][0][0]
    fragments = FrozenFragments.load([]).append_current(identity, ChatMessage("user", "old body"))
    publish = await _prepare(repository, context, fragments, reason="bootstrap")
    async with database.immediate_session() as writer:
        await publish(writer)
    key = context.read_version.conversation_id
    original = await repository.read(key)
    source = _source(original)
    repository.remember_history_source(key, source)
    async with database.immediate_session() as writer:
        await writer.execute(
            update(ChatEventModel).where(ChatEventModel.id == identity).values(content="edited")
        )
    assert await repository.read(key) is None
    assert repository.history_source(key) is None
    # Existing captures remain immutable until their original consumer exits.
    assert original.items()[0]["message"]["content"] == "old body"


async def test_new_epoch_and_late_old_source_remember_do_not_pin_an_unrelated_prefix(database):
    _ledger, context, _seed = await scene(database, 2)
    repository = PromptProjectionRepository(database)
    identity = context.history_fragments[0][0][0]
    first = FrozenFragments.load([]).append_current(identity, ChatMessage("user", "old body"))
    publish = await _prepare(repository, context, first, reason="bootstrap")
    async with database.immediate_session() as writer:
        await publish(writer)
    key = context.read_version.conversation_id
    original = await repository.read(key)
    source = _source(original)
    repository.remember_history_source(key, source)
    replacement = FrozenFragments.load([]).append_current(
        identity, ChatMessage("user", "new epoch body")
    )
    publish = await _prepare(repository, context, replacement, original, reason="compact")
    async with database.immediate_session() as writer:
        await publish(writer)
    current = await repository.read(key)
    assert current.epoch_id != original.epoch_id and repository.history_source(key) is None
    repository.remember_history_source(key, source)
    assert repository.history_source(key) is None
    assert await repository.read(key) is current
    assert current.items()[0]["message"]["content"] == "new epoch body"
    assert source.reconcile(original)


@pytest.mark.parametrize("visibility", ["sparse", "empty", "all"])
async def test_cold_selection_range_prefilter_preserves_exact_sparse_membership(
    database, monkeypatch, visibility
):
    _ledger, context, _seed = await scene(database, 3)
    version = context.read_version
    identities = sorted(context.visible_event_ids)
    groups = [
        (identities[0],),
        (identities[1],),
        (identities[0], identities[2]),
        (),
        (identities[0], 999999),
    ]
    items = [
        {"event_ids": list(ids), "message": {"role": "user", "content": str(index)}}
        for index, ids in enumerate(groups)
    ]
    async with database.immediate_session() as writer:
        await writer.execute(
            insert(ContextSelectionModel),
            [
                {
                    "view_key": version.conversation_id,
                    "conversation_id": version.conversation_id,
                    "generation": version.generation,
                    "source_key": f"sparse-{index}",
                    "event_ids_json": json.dumps(item["event_ids"]),
                    "min_event_id": min(groups[index], default=0),
                    "max_event_id": max(groups[index], default=0),
                    "payload_json": json.dumps(item),
                    "created_at": datetime.now(UTC),
                }
                for index, item in enumerate(items)
            ],
        )
    decode = json.loads

    def no_outside_group(value, *args, **kwargs):
        if visibility != "all":
            assert value != json.dumps(list(groups[-1])), (
                "decoded a group beyond the captured range"
            )
        return decode(value, *args, **kwargs)

    monkeypatch.setattr(json, "loads", no_outside_group)
    selected = await PromptProjectionRepository(database).selected(
        view_key=version.conversation_id,
        conversation_id=version.conversation_id,
        generation=version.generation,
        visible_event_ids=(
            frozenset((identities[0], identities[2]))
            if visibility == "sparse"
            else frozenset()
            if visibility == "empty"
            else None
        ),
    )
    assert selected == (
        [items[0], items[2], items[3]]
        if visibility == "sparse"
        else [items[3]]
        if visibility == "empty"
        else items
    )
