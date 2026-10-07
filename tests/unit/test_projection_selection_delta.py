"""Real SQLite selection deltas preserve sources, epochs and atomic ownership."""

import asyncio
import hashlib
import json
from datetime import UTC, datetime
from time import perf_counter

import pytest
from sqlalchemy import delete, event, select, update
from tests.support.projection_sql_counts import capture_sql, parameter_bytes, timing_summary
from tests.support.social_identity_cases import social_env
from tests.unit.test_context_observation_sources import add_clue, summary

from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel
from qq_ai_bot.conversation.frozen_fragments import FrozenFragments
from qq_ai_bot.conversation.observation_models import ContextObservationModel, ContextSelectionModel
from qq_ai_bot.conversation.observations import ContextObservationRepository
from qq_ai_bot.conversation.projection_models import PromptProjectionModel
from qq_ai_bot.conversation.projections import ProjectionConflict, PromptProjectionRepository
from qq_ai_bot.domain.messages import ChatMessage
from qq_ai_bot.execution_trace.db_models import ExecutionTraceStateModel
from qq_ai_bot.identity.canonical_repository import ensure_space
from qq_ai_bot.tool_results.artifacts import ToolArtifactRepository
from qq_ai_bot.tool_results.schema import artifact_refs


async def scene(database, tmp_path):
    env = await social_env(database, tmp_path)
    async with database.sessions() as reader:
        source = await reader.get(CanonicalConversationModel, env.context.conversation_id)
        arguments = dict(
            view_key="a" * 64,
            conversation_id=source.id,
            generation=source.generation,
            expected_source_revision=source.prompt_source_revision,
            starts_after_event_id=source.starts_after_event_id,
            context_key="b" * 64,
            contract_revision="c" * 64,
            actor_id=env.person,
            read_scope="main",
        )
    return env, PromptProjectionRepository(database), arguments


def chat(count):
    fragments = FrozenFragments.load([])
    for index in range(1, count + 1):
        fragments = fragments.append_current(index, ChatMessage("user", f"chat-{index}"))
    return fragments


async def test_delta_inserts_only_new_payload_and_keeps_old_selection_ids(database, tmp_path):
    _env, repository, arguments = await scene(database, tmp_path)
    first = await repository.commit(
        **arguments, items=list(chat(20).items), rebuild_reason="bootstrap"
    )
    async with database.sessions() as reader:
        before = list((await reader.execute(select(ContextSelectionModel.id))).scalars())
    samples = []
    for _ in range(12):
        started = perf_counter()
        await repository.prepare_commit(
            **arguments,
            items=list(chat(21).items),
            expected_epoch=first.epoch_id,
            expected_revision=first.revision,
        )
        samples.append(perf_counter() - started)
    print("projection_prepare_delta", timing_summary(samples))
    with capture_sql(database) as statements:
        await repository.commit(
            **arguments,
            items=list(chat(21).items),
            expected_epoch=first.epoch_id,
            expected_revision=first.revision,
        )
    inserts = [
        params
        for sql, params in statements
        if sql.startswith("INSERT INTO model_context_selections")
    ]
    assert len(inserts) == 1
    payloads = [
        value for value in inserts[0] if isinstance(value, str) and value.startswith('{"kind"')
    ]
    assert len(payloads) == 1 and json.loads(payloads[0])["event_ids"] == [21]
    assert parameter_bytes(payloads) == len(payloads[0].encode("utf-8"))
    print(
        "selection_delta_io",
        {
            "old_rows": 20,
            "new_rows": 1,
            "insert_statements": len(inserts),
            "payload_bytes": parameter_bytes(payloads),
        },
    )
    # Old selection bodies are not reloaded to compare the already frozen prefix.
    assert not any(
        sql.startswith("SELECT") and "model_context_selections.payload_json" in sql
        for sql, _ in statements
    )
    async with database.sessions() as reader:
        after = list((await reader.execute(select(ContextSelectionModel.id))).scalars())
    assert after[:20] == before and len(after) == 21


async def test_wide_bootstrap_keeps_each_selection_insert_under_sqlite_variable_budget(
    database, tmp_path
):
    _env, repository, arguments = await scene(database, tmp_path)
    inserts = []

    def capture(_conn, _cursor, sql, parameters, *_args):
        if sql.startswith("INSERT INTO model_context_selections"):
            inserts.append(parameters)

    event.listen(database.engine.sync_engine, "before_cursor_execute", capture)
    try:
        await repository.commit(
            **arguments, items=list(chat(130).items), rebuild_reason="bootstrap"
        )
    finally:
        event.remove(database.engine.sync_engine, "before_cursor_execute", capture)
    assert inserts and all(len(parameters) <= 999 for parameters in inserts)
    async with database.sessions() as reader:
        selected = (
            await reader.scalars(
                select(ContextSelectionModel.payload_json).order_by(ContextSelectionModel.id)
            )
        ).all()
    assert [json.loads(payload)["event_ids"] for payload in selected] == [
        [i] for i in range(1, 131)
    ]


async def test_same_view_real_concurrent_publications_have_one_cas_winner(database, tmp_path):
    _env, repository, arguments = await scene(database, tmp_path)
    initial = await repository.commit(
        **arguments, items=list(chat(1).items), rebuild_reason="bootstrap"
    )
    options = dict(
        **arguments,
        expected_epoch=initial.epoch_id,
        expected_revision=initial.revision,
    )
    plans = [await repository.prepare_commit(**options, items=list(chat(n).items)) for n in (2, 3)]

    async def publish(plan):
        async with database.immediate_session() as writer:
            return await plan(writer)

    results = await asyncio.gather(*(publish(plan) for plan in plans), return_exceptions=True)
    assert sum(isinstance(result, ProjectionConflict) for result in results) == 1
    current = await repository.read(arguments["view_key"])
    async with database.sessions() as reader:
        selected = (await reader.scalars(select(ContextSelectionModel.payload_json))).all()
    assert len(selected) == len(current.items())


@pytest.mark.parametrize("change", ["owner", "generation", "privacy", "parent"])
async def test_prepared_delta_rejects_changed_sources_on_second_connection(
    database, tmp_path, change
):
    env, repository, arguments = await scene(database, tmp_path)
    await add_clue(database, env, "parent", size=1)
    fragments = chat(1).append_observation("parent", 1, ChatMessage("user", "note"))
    first = await repository.commit(
        **arguments, items=list(fragments.items), rebuild_reason="bootstrap"
    )
    plan = await repository.prepare_commit(
        **arguments,
        items=list(fragments.append_current(2, ChatMessage("user", "new-chat")).items),
        expected_epoch=first.epoch_id,
        expected_revision=first.revision,
    )
    async with database.immediate_session() as competing:
        if change == "owner":
            replacement_owner = await ensure_space(competing, "rebound-space")
            await competing.execute(
                update(CanonicalConversationModel)
                .where(CanonicalConversationModel.id == arguments["conversation_id"])
                .values(space_id=replacement_owner)
            )
        elif change == "generation":
            await competing.execute(
                update(CanonicalConversationModel)
                .where(CanonicalConversationModel.id == arguments["conversation_id"])
                .values(generation=2)
            )
        elif change == "privacy":
            competing.add(ExecutionTraceStateModel(id=1, privacy_generation=1))
        else:
            await competing.execute(
                delete(ContextObservationModel).where(ContextObservationModel.id == "parent")
            )
    with pytest.raises(ProjectionConflict):
        async with database.immediate_session() as writer:
            await plan(writer)
    async with database.sessions() as reader:
        payloads = (await reader.scalars(select(ContextSelectionModel.payload_json))).all()
    assert all("new-chat" not in payload for payload in payloads)


async def test_capacity_epoch_retains_first_snapshot_selection_for_same_chat(database, tmp_path):
    env, repository, arguments = await scene(database, tmp_path)
    await add_clue(database, env, "snapshot-parent", size=1)
    original = chat(1).items[0].copy()
    original.update(observation_id="snapshot-parent", observation_version=1)
    first = await repository.commit(**arguments, items=[original], rebuild_reason="bootstrap")
    raw = chat(1).items[0]
    replaced = await repository.commit(
        **arguments,
        items=[raw],
        rebuild_reason="capacity",
        expected_epoch=first.epoch_id,
        expected_revision=first.revision,
    )
    assert replaced.items() == [raw]
    async with database.sessions() as reader:
        selected = (await reader.scalars(select(ContextSelectionModel.payload_json))).one()
    assert json.loads(selected) == original
    # The adopted raw prefix remains legal in subsequent ordinary appends.
    await repository.commit(
        **arguments,
        items=list(chat(2).items),
        expected_epoch=replaced.epoch_id,
        expected_revision=replaced.revision,
    )


async def test_existing_nonprefix_selection_content_conflict_is_not_ignored(database, tmp_path):
    _env, repository, arguments = await scene(database, tmp_path)
    first = await repository.commit(
        **arguments, items=list(chat(1).items), rebuild_reason="bootstrap"
    )
    item = chat(2).items[-1]
    # Model an unexpected stored representation outside the proven frozen prefix.
    async with database.immediate_session() as writer:
        writer.add(
            ContextSelectionModel(
                view_key=arguments["view_key"],
                conversation_id=arguments["conversation_id"],
                generation=arguments["generation"],
                actor_id=arguments["actor_id"],
                read_scope=arguments["read_scope"],
                source_key=hashlib.sha256(json.dumps([2]).encode()).hexdigest(),
                event_ids_json="[2]",
                observation_sources_json="[]",
                payload_json=json.dumps(
                    {**item, "message": {"role": "user", "content": "changed"}}
                ),
                created_at=datetime.now(UTC),
            )
        )
    with pytest.raises(ProjectionConflict, match="immutable"):
        await repository.commit(
            **arguments,
            items=list(chat(2).items),
            expected_epoch=first.epoch_id,
            expected_revision=first.revision,
        )


@pytest.mark.parametrize("difference", ["content", "observation"])
async def test_same_publication_key_collision_never_discards_a_different_item(
    database, tmp_path, monkeypatch, difference
):
    env, repository, arguments = await scene(database, tmp_path)
    await add_clue(database, env, "a", size=1)
    await add_clue(database, env, "b", size=1)
    first = FrozenFragments.load([]).append_observation("a", 1, ChatMessage("user", "same"))
    second = dict(first.items[0])
    if difference == "content":
        second["message"] = {"role": "user", "content": "changed"}
    else:
        second["observation_id"] = "b"
    # Exercise the identity collision guard independently of the hash algorithm.
    monkeypatch.setattr("qq_ai_bot.conversation.projections._selection_key", lambda _item: "x")
    with pytest.raises(ProjectionConflict, match="identity conflict"):
        await repository.commit(
            **arguments, items=[first.items[0], second], rebuild_reason="bootstrap"
        )
    async with database.sessions() as reader:
        assert (await reader.scalars(select(ContextSelectionModel.id))).first() is None


async def test_each_selected_summary_retains_its_own_parent_handles(database, tmp_path):
    env, repository, arguments = await scene(database, tmp_path)
    await add_clue(database, env, "parent", size=1)
    artifact_store = ToolArtifactRepository(database, tmp_path / "artifacts", retention_seconds=60)
    handle = await artifact_store.write_artifact(
        provider_id="core", tool_name="search", content="source", media_type="text/plain"
    )
    common = dict(
        conversation_id=arguments["conversation_id"],
        generation=1,
        actor_id=env.person,
        read_scope="main",
        version=1,
        parent_sources_json='[["parent",1]]',
        summary_view_key=arguments["view_key"],
        created_at=datetime.now(UTC),
    )
    async with database.immediate_session() as writer:
        for identity in ("root-a", "root-b"):
            writer.add(
                ContextObservationModel(
                    **common,
                    id=identity,
                    source_key=identity,
                    payload_json=json.dumps({"text": identity}),
                )
            )
        await artifact_store.add_refs(writer, "observation", "parent", (handle,))
        # Only root A owns the handle. A union of both successor owners is unsafe.
        await artifact_store.add_refs(writer, "observation", "root-a", (handle,))
    fragments = FrozenFragments.load([])
    for identity in ("root-a", "root-b"):
        fragments = fragments.append_observation(identity, 1, ChatMessage("user", identity))
    with pytest.raises(ProjectionConflict, match="artifact transfer"):
        await repository.commit(
            **arguments, items=list(fragments.items), rebuild_reason="bootstrap"
        )
    async with database.sessions() as reader:
        assert (await reader.scalars(select(ContextSelectionModel.id))).first() is None
        assert (await reader.scalars(select(PromptProjectionModel.view_key))).first() is None
        assert ("parent", handle) in (
            await reader.execute(select(artifact_refs.c.owner_id, artifact_refs.c.handle_id))
        ).all()


async def test_summary_transfer_rejects_late_parent_handle_without_successor_owner(
    database, tmp_path
):
    env, repository, arguments = await scene(database, tmp_path)
    await add_clue(database, env, "parent", size=1)
    observations = ContextObservationRepository(database)
    rows = await observations.read(
        conversation_id=arguments["conversation_id"],
        generation=1,
        actor_id=env.person,
        read_scope="main",
    )
    artifact_store = ToolArtifactRepository(database, tmp_path / "artifacts", retention_seconds=60)
    first_handle = await artifact_store.write_artifact(
        provider_id="core", tool_name="search", content="original", media_type="text/plain"
    )
    async with database.immediate_session() as writer:
        await artifact_store.add_refs(writer, "observation", "parent", (first_handle,))
    paid = await observations.publish_scope_summary(
        view_key=arguments["view_key"],
        observations=rows,
        payload=summary(rows),
        conversation_id=arguments["conversation_id"],
        generation=1,
        actor_id=env.person,
        read_scope="main",
        expected_source_revision=arguments["expected_source_revision"],
    )
    fragments = FrozenFragments.load([]).append_observation(paid.id, paid.version, paid.message())
    plan = await repository.prepare_commit(
        **arguments, items=list(fragments.items), rebuild_reason="bootstrap"
    )
    late_handle = await artifact_store.write_artifact(
        provider_id="core", tool_name="search", content="late", media_type="text/plain"
    )
    async with database.immediate_session() as competing:
        await artifact_store.add_refs(competing, "observation", "parent", (late_handle,))
    with pytest.raises(ProjectionConflict, match="artifact transfer"):
        async with database.immediate_session() as writer:
            await plan(writer)
    async with database.sessions() as reader:
        refs = (
            await reader.execute(select(artifact_refs.c.owner_id, artifact_refs.c.handle_id))
        ).all()
        assert ("parent", late_handle) in refs and ("parent", first_handle) in refs
        assert (await reader.scalars(select(ContextSelectionModel.id))).first() is None
        assert (await reader.scalars(select(PromptProjectionModel.view_key))).first() is None


async def test_selected_summary_repeat_does_not_delete_parent_refs_again(database, tmp_path):
    env, repository, arguments = await scene(database, tmp_path)
    await add_clue(database, env, "parent", size=1)
    observations = ContextObservationRepository(database)
    rows = await observations.read(
        conversation_id=arguments["conversation_id"],
        generation=1,
        actor_id=env.person,
        read_scope="main",
    )
    paid = await observations.publish_scope_summary(
        view_key=arguments["view_key"],
        observations=rows,
        payload=summary(rows),
        conversation_id=arguments["conversation_id"],
        generation=1,
        actor_id=env.person,
        read_scope="main",
        expected_source_revision=arguments["expected_source_revision"],
    )
    fragments = FrozenFragments.load([]).append_observation(paid.id, paid.version, paid.message())
    first = await repository.commit(
        **arguments, items=list(fragments.items), rebuild_reason="bootstrap"
    )
    with capture_sql(database) as statements:
        await repository.commit(
            **arguments,
            items=list(fragments.items),
            expected_epoch=first.epoch_id,
            expected_revision=first.revision,
        )
    assert not any(
        sql.startswith(("DELETE FROM tool_artifact_refs", "INSERT INTO model_context_selections"))
        for sql, _ in statements
    )
