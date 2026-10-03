"""Admitted observation coverage is durable, private, and owns its raw references."""

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import delete, func, select, update
from tests.support.social_identity_cases import social_env
from tests.unit.test_context_note_artifact_lifecycle import admitted, note

from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel
from qq_ai_bot.conversation.observation_models import ContextObservationModel, ContextSelectionModel
from qq_ai_bot.conversation.observations import ContextObservationRepository
from qq_ai_bot.conversation.projection_models import PromptProjectionModel
from qq_ai_bot.conversation.projections import ProjectionConflict, PromptProjectionRepository
from qq_ai_bot.domain.conversations import ConversationScope
from qq_ai_bot.domain.messages import ChatMessage
from qq_ai_bot.execution_trace.db_models import ExecutionTraceStateModel
from qq_ai_bot.llm.base import LLMError
from qq_ai_bot.mcp.artifact_schema import artifact_refs
from qq_ai_bot.mcp.repository import ToolArtifactRepository
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.event_repository import EventLedgerRepository
from qq_ai_bot.persistence.models import ChatEventModel, ToolArtifactModel
from qq_ai_bot.persistence.schema_guard import require_canonical_schema
from qq_ai_bot.runtime.work_repository import WorkCapacityError, WorkConflict, WorkRepository
from qq_ai_bot.services.context_assembler import AssembledContext, ContextMetrics
from qq_ai_bot.services.history_projection import prepare_history
from qq_ai_bot.time.models import TimeContext


async def context_for(database):
    ledger = EventLedgerRepository(database)
    version, events = await ledger.read_scope_context(
        ConversationScope.group("80001", "20001"), limit=256
    )
    fragments = tuple(
        ((event.id,), ChatMessage(role="user", content=event.content)) for event in events
    )
    now = datetime.now(UTC)
    return AssembledContext(
        metadata_payload={},
        history_messages=tuple(message for _, message in fragments),
        current_message=ChatMessage(role="user", content="current"),
        recent_delivery=(),
        current_time=TimeContext(now, now, "UTC"),
        current_relationship=None,
        metrics=ContextMetrics(0, 0, 0, 0, False),
        read_version=version,
        history_fragments=fragments,
        history_event_fragments=fragments,
        visible_event_ids=frozenset(event.id for event in events),
        prompt_raw_tail_end_event_id=events[-1].id,
    )


async def add_clue(database, env, identity, size=800):
    async with database.sessions() as session, session.begin():
        session.add(
            ContextObservationModel(
                id=identity,
                conversation_id=env.context.conversation_id,
                generation=1,
                actor_id=env.person,
                read_scope="main",
                source_key="fixture:" + identity,
                version=1,
                payload_json=json.dumps({"clue": identity, "text": "x" * size}),
                created_at=datetime.now(UTC),
            )
        )


def summary(rows):
    return {
        "version": 1,
        "facts": [
            {"text": "Preserved research clues", "refs": ["observation:" + row.id for row in rows]}
        ],
        "unresolved": [],
        "next_steps": [],
    }


async def prepare(database, env, context, callback, limit=550):
    return await prepare_history(
        PromptProjectionRepository(database),
        context,
        view_key="a" * 64,
        context_key="b" * 64,
        contract_revision="c" * 64,
        actor_id=env.person,
        read_scope="main",
        history_fits=lambda _: True,
        context_fits=lambda candidate: (
            sum(len(m.content or "") for m in candidate.history_messages) < limit
        ),
        summarize_observations=callback,
    )


async def test_paid_scope_summary_not_coverage_until_dispatch_then_survives_restart(
    database, tmp_path
):
    env = await social_env(database, tmp_path)
    await add_clue(database, env, "clue-a")
    await add_clue(database, env, "clue-b")
    context = await context_for(database)
    callback = AsyncMock(side_effect=summary)
    prepared = await prepare(database, env, context, callback)
    repository = ContextObservationRepository(database)
    scope = dict(
        conversation_id=env.context.conversation_id,
        generation=1,
        actor_id=env.person,
        read_scope="main",
        view_key="a" * 64,
    )
    assert {row.id for row in await repository.read(**scope)} == {"clue-a", "clue-b"}
    async with database.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(ContextSelectionModel)) == 0
    again = await prepare(database, env, context, callback)
    assert callback.await_count == 1
    assert again.fragments.items == prepared.fragments.items
    snapshot = await again.commit(again.fragments)
    selected = await repository.read(**scope)
    assert len(selected) == 1 and selected[0].parent_sources == (("clue-a", 1), ("clue-b", 1))
    await database.engine.dispose()
    reopened = await prepare(database, env, await context_for(database), callback)
    assert reopened.previous.epoch_id == snapshot.epoch_id
    assert reopened.fragments.items == again.fragments.items
    assert callback.await_count == 1
    assert await repository.read(**{**scope, "actor_id": "other"}) == ()
    assert await repository.read(**{**scope, "read_scope": "limited"}) == ()


async def test_no_fit_keeps_raw_and_paid_candidate_is_not_repeated(database, tmp_path):
    env = await social_env(database, tmp_path)
    await add_clue(database, env, "clue-a")
    callback = AsyncMock(side_effect=summary)
    first = await prepare(database, env, await context_for(database), callback, limit=1)
    second = await prepare(database, env, await context_for(database), callback, limit=1)
    assert (
        first.fragments.observation_sources
        == second.fragments.observation_sources
        == (("clue-a", 1),)
    )
    assert callback.await_count == 1


async def test_fit_does_not_invoke_summary_and_real_parent_delete_rejects_prepared_dispatch(
    database, tmp_path
):
    env = await social_env(database, tmp_path)
    await add_clue(database, env, "clue-a", size=1)
    callback = AsyncMock(side_effect=summary)
    prepared = await prepare(database, env, await context_for(database), callback)
    callback.assert_not_awaited()
    async with database.sessions() as session, session.begin():
        await session.execute(
            delete(ContextObservationModel).where(ContextObservationModel.id == "clue-a")
        )
    with pytest.raises(ProjectionConflict):
        await prepared.commit(prepared.fragments)
    assert not await EventLedgerRepository(database).read_version_matches(
        prepared.context.read_version
    )


@pytest.mark.parametrize("bad", ["foreign", "missing_parent"])
async def test_summary_requires_actual_sources_and_complete_coverage(database, tmp_path, bad):
    env = await social_env(database, tmp_path)
    await add_clue(database, env, "clue-a")
    await add_clue(database, env, "clue-b")

    def invalid(rows):
        payload = summary(rows)
        payload["facts"][0]["refs"] = (
            ["observation:foreign"] if bad == "foreign" else ["observation:clue-a"]
        )
        return payload

    with pytest.raises(ValueError, match="observation_summary"):
        await prepare(database, env, await context_for(database), AsyncMock(side_effect=invalid))
    async with database.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(ContextObservationModel)) == 2


async def test_summary_transfers_artifact_owner_only_with_selection_and_reset_releases_last_owner(
    database, tmp_path
):
    env = await social_env(database, tmp_path)
    await add_clue(database, env, "clue-a")
    store = ToolArtifactRepository(database, tmp_path / "research", retention_seconds=60)
    handle = await store.write_artifact(
        provider_id="core", tool_name="web_search", content="raw research", media_type="text/plain"
    )
    async with database.immediate_session() as session:
        await store.add_refs(session, "observation", "clue-a", (handle,))
        await session.execute(
            update(ToolArtifactModel)
            .where(ToolArtifactModel.handle_id == handle)
            .values(expires_at=datetime.now(UTC) - timedelta(days=1))
        )
    prepared = await prepare(
        database, env, await context_for(database), AsyncMock(side_effect=summary)
    )
    async with database.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(artifact_refs)) == 2
    await prepared.commit(prepared.fragments)
    async with database.sessions() as session:
        owners = (await session.scalars(select(artifact_refs.c.owner_id))).all()
    assert owners == [prepared.fragments.observation_sources[0][0]]
    assert await store.cleanup() == 0
    async with database.sessions() as session, session.begin():
        await session.execute(
            update(CanonicalConversationModel)
            .where(CanonicalConversationModel.id == env.context.conversation_id)
            .values(generation=2)
        )
    assert await store.cleanup() == 1


async def test_saved_work_note_publishes_once_in_original_scope(database, tmp_path):
    control, access = await admitted(database, tmp_path)
    assert json.loads(
        await control.execute(
            "task_control", {"action": "update", "context_note": note()}, "note-1"
        )
    )["ok"]
    repository = ContextObservationRepository(database)
    rows = await repository.read(
        conversation_id=access.conversation_id,
        generation=1,
        actor_id=access.actor_person_id,
        read_scope=access.read_scope,
    )
    assert len(rows) == 1
    saved = await control.repository.get(control.current["id"])
    raw_note = json.loads(saved["checkpoint_json"])["context_note"]
    assert await repository.publish_note(saved, 1, raw_note["payload"]) == rows[0].id
    assert (
        await repository.read(
            conversation_id=access.conversation_id,
            generation=1,
            actor_id="untrusted",
            read_scope=access.read_scope,
        )
        == ()
    )


async def test_migration_0089_installs_current_sources_and_preserves_0088_work(
    tmp_path, monkeypatch
):
    path = tmp_path / "migrated.sqlite3"
    url = "sqlite+aiosqlite:///" + path.as_posix()
    monkeypatch.setenv("DATABASE_URL", url)
    await asyncio.to_thread(command.upgrade, Config("alembic.ini"), "0088")
    old_database = Database(url)
    env = await social_env(old_database, tmp_path / "old")
    repository = WorkRepository(old_database)
    lease = await repository.acquire(env.context.conversation_id, 1)
    original = await repository.accept(
        lease,
        source_key="old-fact",
        source={"actor_person_id": env.person},
        goal="Original goal and budget",
    )
    await old_database.engine.dispose()
    await asyncio.to_thread(command.upgrade, Config("alembic.ini"), "head")
    await require_canonical_schema(url)
    assert await repository.get(original["id"]) == original
    await old_database.engine.dispose()


async def test_real_chat_edit_keeps_valid_summary_coverage_and_parent_delete_retires_descendants(
    database, tmp_path
):
    env = await social_env(database, tmp_path)
    await add_clue(database, env, "clue-a")
    callback = AsyncMock(side_effect=summary)
    first = await prepare(database, env, await context_for(database), callback)
    await first.commit(first.fragments)
    first_summary = first.fragments.observation_sources[0][0]
    await add_clue(database, env, "clue-b")
    second = await prepare(database, env, await context_for(database), callback)
    await second.commit(second.fragments)
    second_summary = second.fragments.observation_sources[0][0]
    async with database.sessions() as session, session.begin():
        await session.execute(
            update(ChatEventModel)
            .where(ChatEventModel.canonical_conversation_id == env.context.conversation_id)
            .values(content="edited chat")
        )
    third = await prepare(database, env, await context_for(database), callback)
    assert third.fragments.observation_sources == ((second_summary, 1),)
    assert callback.await_count == 2
    await third.commit(third.fragments)
    async with database.sessions() as session, session.begin():
        await session.execute(
            delete(ContextObservationModel).where(ContextObservationModel.id == "clue-a")
        )
    async with database.sessions() as session:
        assert await session.get(ContextObservationModel, first_summary) is None
        assert await session.get(ContextObservationModel, second_summary) is None
    assert not await ContextObservationRepository(database).validate_sources(
        env.context.conversation_id, 1, env.person, "main", ((second_summary, 1),)
    )


async def test_normal_work_retention_preserves_observation_source_but_privacy_purge_removes_it(
    database, tmp_path
):
    control, access = await admitted(database, tmp_path)
    assert json.loads(
        await control.execute(
            "task_control", {"action": "update", "context_note": note()}, "note-1"
        )
    )["ok"]
    from qq_ai_bot.runtime.work_schema_v1 import work

    async with database.sessions() as session, session.begin():
        await session.execute(delete(work).where(work.c.id == control.current["id"]))
    repository = ContextObservationRepository(database)
    kwargs = dict(
        conversation_id=access.conversation_id,
        generation=1,
        actor_id=access.actor_person_id,
        read_scope=access.read_scope,
    )
    rows = await repository.read(**kwargs)
    assert len(rows) == 1
    async with database.sessions() as session:
        assert (await session.get(ContextObservationModel, rows[0].id)).source_work_id is None
    async with database.immediate_session() as session:
        await WorkRepository.purge_scope(session, access.conversation_id)
    assert await repository.read(**kwargs) == ()


async def test_snapshot_same_dispatch_does_not_duplicate_A_and_summary_preserves_raw_chat(
    database, tmp_path
):
    env = await social_env(database, tmp_path)
    base = await context_for(database)
    event_id = next(iter(base.visible_event_ids))
    current = replace(
        base,
        current_event_id=event_id,
        history_messages=(),
        history_fragments=(),
        history_event_fragments=(),
    )
    prepared = await prepare(database, env, current, None)
    compiled = prepared.fragments.append_current(
        event_id, ChatMessage("user", "compiled A " + "x" * 1200)
    )
    snapshot = {"context": {"profile": "private-map" + "y" * 900}}
    first = await prepared.commit(compiled, current_snapshot=snapshot)
    assert len(first.items()) == 1
    from qq_ai_bot.conversation.frozen_fragments import FrozenFragments

    assert FrozenFragments.load(first.items()).messages() == compiled.messages()
    assert await prepared.commit(compiled, current_snapshot=snapshot) == first
    callback = AsyncMock(side_effect=summary)
    compacted = await prepare(database, env, base, callback)
    assert compacted.fragments.event_ids == {event_id}
    assert any("hello" in (message.content or "") for message in compacted.fragments.messages())
    assert callback.await_count == 1
    supplied = callback.call_args.args[0]
    assert len(supplied) == 1 and json.loads(supplied[0].payload_json) == snapshot
    assert "compiled A" not in supplied[0].payload_json
    await compacted.commit(compacted.fragments)
    different_view = await ContextObservationRepository(database).read(
        conversation_id=env.context.conversation_id,
        generation=1,
        actor_id=env.person,
        read_scope="main",
        view_key="d" * 64,
    )
    assert different_view == ()


async def test_snapshot_without_event_cannot_relabel_previous_frozen_envelope(database, tmp_path):
    env = await social_env(database, tmp_path)
    context = await context_for(database)
    prepared = await prepare(database, env, context, None)
    fragments = prepared.fragments.append_current(None, ChatMessage("user", "older envelope"))
    before = await prepared.commit(fragments)
    next_prepared = await prepare(database, env, context, None)
    assert list(next_prepared.fragments.items) == before.items()
    after = await next_prepared.commit(
        next_prepared.fragments, current_snapshot={"context": {"new": "data"}}
    )
    assert after.items() == before.items()
    assert after.epoch_id == before.epoch_id


async def test_self_new_envelope_has_exact_snapshot_pointer_and_next_read_deduplicates(
    database, tmp_path
):
    env = await social_env(database, tmp_path)
    context = replace(await context_for(database), projection_scope="self_initiative")
    repository = PromptProjectionRepository(database)
    kwargs = dict(
        view_key="d" * 64,
        context_key="e" * 64,
        contract_revision="f" * 64,
        actor_id="self",
        read_scope="main",
        history_fits=lambda _: True,
    )
    prepared = await prepare_history(repository, context, **kwargs)
    compiled = prepared.fragments.append_current(None, ChatMessage("user", "compiled SELF input"))
    snapshot = {"context": {"profile": "SELF observed profile"}}
    actual = await prepared.commit(compiled, current_snapshot=snapshot)
    assert actual.items()[:-1] == list(compiled.items[:-1])
    assert actual.items()[-1]["message"] == compiled.items[-1]["message"]
    assert "observation_id" in actual.items()[-1]
    rows = await ContextObservationRepository(database).read(
        conversation_id=env.context.conversation_id,
        generation=1,
        actor_id="self",
        read_scope="main",
        view_key="d" * 64,
    )
    assert len(rows) == 1 and json.loads(rows[0].payload_json) == snapshot
    again = await prepare_history(repository, context, **kwargs)
    assert list(again.fragments.items) == actual.items()


async def test_failed_snapshot_CAS_does_not_publish_and_global_privacy_retires_old_notes(
    database, tmp_path
):
    env = await social_env(database, tmp_path)
    context = await context_for(database)
    prepared = await prepare(database, env, context, None)
    async with database.sessions() as session, session.begin():
        await session.execute(
            update(CanonicalConversationModel)
            .where(CanonicalConversationModel.id == env.context.conversation_id)
            .values(prompt_source_revision=1)
        )
    with pytest.raises(ProjectionConflict):
        await prepared.commit(
            prepared.fragments, current_snapshot={"context": {"profile": "private"}}
        )
    async with database.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(ContextObservationModel)) == 0
    await add_clue(database, env, "old-private")
    before = await prepare(database, env, await context_for(database), None, limit=5000)
    async with database.sessions() as session, session.begin():
        session.add(ExecutionTraceStateModel(id=1, privacy_generation=1))
    assert (
        await ContextObservationRepository(database).read(
            conversation_id=env.context.conversation_id,
            generation=1,
            actor_id=env.person,
            read_scope="main",
            view_key="a" * 64,
        )
        == ()
    )
    with pytest.raises(ProjectionConflict):
        await before.commit(before.fragments)


@pytest.mark.parametrize(
    "failure", [WorkCapacityError("summary_window"), LLMError("provider_failure")]
)
async def test_optional_summary_is_not_requested_while_original_input_fits(
    database, tmp_path, failure
):
    env = await social_env(database, tmp_path)
    await add_clue(database, env, "clue-a")
    context = await context_for(database)
    callback = AsyncMock(side_effect=failure)
    prepared = await prepare_history(
        PromptProjectionRepository(database),
        context,
        view_key="a" * 64,
        context_key="b" * 64,
        contract_revision="c" * 64,
        actor_id=env.person,
        read_scope="main",
        history_fits=lambda _: False,
        context_fits=lambda _: False,
        context_hard_fits=lambda _: True,
        summarize_observations=callback,
    )
    assert prepared.fragments.observation_sources == (("clue-a", 1),)
    await prepared.commit(prepared.fragments)
    callback.assert_not_awaited()


@pytest.mark.parametrize(
    "failure", [WorkConflict("lease"), ProjectionConflict("source"), ValueError("programming")]
)
async def test_optional_summary_does_not_swallow_authority_source_or_programming_failure(
    database, tmp_path, failure
):
    env = await social_env(database, tmp_path)
    await add_clue(database, env, "clue-a")
    with pytest.raises(type(failure)):
        await prepare_history(
            PromptProjectionRepository(database),
            await context_for(database),
            view_key="a" * 64,
            context_key="b" * 64,
            contract_revision="c" * 64,
            actor_id=env.person,
            read_scope="main",
            history_fits=lambda _: False,
            context_fits=lambda _: False,
            context_hard_fits=lambda _: False,
            summarize_observations=AsyncMock(side_effect=failure),
        )


@pytest.mark.parametrize("ready", [False, True])
async def test_required_summary_can_fit_real_capacity_above_soft_target(database, tmp_path, ready):
    env = await social_env(database, tmp_path)
    await add_clue(database, env, "large-clue", size=3000)
    repository = ContextObservationRepository(database)
    context = await context_for(database)
    callback = AsyncMock(side_effect=summary)
    if ready:
        observations = await repository.read(
            conversation_id=env.context.conversation_id,
            generation=1,
            actor_id=env.person,
            read_scope="main",
            view_key="a" * 64,
        )
        await repository.publish_scope_summary(
            view_key="a" * 64,
            observations=observations,
            payload=summary(observations),
            conversation_id=env.context.conversation_id,
            generation=1,
            actor_id=env.person,
            read_scope="main",
            expected_source_revision=context.read_version.prompt_source_revision,
        )
    prepared = await prepare_history(
        PromptProjectionRepository(database),
        context,
        view_key="a" * 64,
        context_key="b" * 64,
        contract_revision="c" * 64,
        actor_id=env.person,
        read_scope="main",
        history_fits=lambda _: True,
        context_fits=lambda _: False,
        context_hard_fits=lambda candidate: (
            sum(len(m.content or "") for m in candidate.history_messages) < 2000
        ),
        summarize_observations=callback,
    )
    assert "Preserved research clues" in str(prepared.fragments.messages())
    assert "x" * 3000 not in str(prepared.fragments.messages())
    assert callback.await_count == (0 if ready else 1)
    await prepared.commit(prepared.fragments)


async def test_prepared_snapshot_source_is_not_observed_or_summarized_before_dispatch(
    database, tmp_path
):
    env = await social_env(database, tmp_path)
    base = await context_for(database)
    event_id = next(iter(base.visible_event_ids))
    current = replace(
        base,
        current_event_id=event_id,
        history_messages=(),
        history_fragments=(),
        history_event_fragments=(),
    )
    prepared = await prepare(database, env, current, None)
    compiled = prepared.fragments.append_current(event_id, ChatMessage("user", "current A"))
    payload = {"context": {"profile": "source-prepared-unobserved"}}
    publication = await prepared.prepare_commit(compiled, current_snapshot=payload)
    async with database.sessions() as reader:
        assert await reader.scalar(select(func.count()).select_from(ContextObservationModel)) == 1
        assert await reader.scalar(select(func.count()).select_from(ContextSelectionModel)) == 0
        assert await reader.scalar(select(func.count()).select_from(PromptProjectionModel)) == 0
    callback = AsyncMock(side_effect=summary)
    unobserved = await prepare(database, env, base, callback)
    assert callback.await_count == 0
    assert "source-prepared-unobserved" not in str(unobserved.fragments.messages())
    # A failed combined writer rolls selection back, preserving the prepared
    # source for an idempotent retry without treating it as read coverage.
    with pytest.raises(RuntimeError, match="journal publication failed"):
        async with database.immediate_session() as writer:
            await publication(writer)
            raise RuntimeError("journal publication failed")
    async with database.sessions() as reader:
        assert await reader.scalar(select(func.count()).select_from(ContextSelectionModel)) == 0
    await prepared.commit(compiled, current_snapshot=payload)
    rows = await ContextObservationRepository(database).read(
        conversation_id=env.context.conversation_id,
        generation=1,
        actor_id=env.person,
        read_scope="main",
        view_key="a" * 64,
    )
    assert len(rows) == 1 and json.loads(rows[0].payload_json) == payload
    async with database.sessions() as reader:
        assert await reader.scalar(select(func.count()).select_from(ContextObservationModel)) == 1
        assert await reader.scalar(select(func.count()).select_from(ContextSelectionModel)) == 1
