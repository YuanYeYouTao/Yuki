"""Snapshot-local proof reuse retains real SQLite publication and authority fences."""

import asyncio
from contextlib import contextmanager
from dataclasses import replace

import pytest
from sqlalchemy import event, select, text, update
from tests.support.social_identity_cases import social_env
from tests.unit.test_work_source_guard import _guard

from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel
from qq_ai_bot.conversation.frozen_fragments import FrozenFragments
from qq_ai_bot.conversation.observation_models import ContextObservationModel
from qq_ai_bot.conversation.projections import ProjectionConflict, PromptProjectionRepository
from qq_ai_bot.domain.conversations import ConversationScope
from qq_ai_bot.domain.messages import ChatMessage
from qq_ai_bot.execution_trace.db_models import ExecutionTraceStateModel
from qq_ai_bot.execution_trace.phases import ModelPhases, current_metrics, current_model_phases
from qq_ai_bot.persistence.models import ChatEventModel
from qq_ai_bot.runtime.work_source_guard import WorkSourceGuard


@contextmanager
def capture(database, monkeypatch):
    statements, sessions = [], []
    original = database.sessions

    def opened(*args, **kwargs):
        sessions.append(True)
        return original(*args, **kwargs)

    def record(_connection, _cursor, statement, *_args):
        statements.append(" ".join(statement.split()))

    with monkeypatch.context() as patch:
        patch.setattr(database, "sessions", opened)
        event.listen(database.engine.sync_engine, "before_cursor_execute", record)
        try:
            yield statements, sessions
        finally:
            event.remove(database.engine.sync_engine, "before_cursor_execute", record)


async def private_scene(database, tmp_path):
    env = await social_env(database, tmp_path)
    receipt = await env.service.writer.append(
        scope=ConversationScope.private(env.bot.self_id, "10001"),
        platform_message_id="private-source-read",
        sender_user_id="10001",
        direction="inbound",
        content="synthetic body " * 8000,
    )
    async with database.sessions() as reader:
        owner = await reader.get(
            CanonicalConversationModel, receipt.event.canonical_conversation_id
        )
        assert owner.kind == "private"
        options = dict(
            view_key="a" * 64,
            context_key="b" * 64,
            contract_revision="c" * 64,
            conversation_id=owner.id,
            generation=owner.generation,
            starts_after_event_id=owner.starts_after_event_id,
            expected_source_revision=owner.prompt_source_revision,
            actor_id=env.person,
            read_scope="main",
            snapshot_event_id=receipt.event.id,
            current_snapshot={"profile": "synthetic frozen profile"},
            items=list(
                FrozenFragments.load([])
                .append_current(receipt.event.id, ChatMessage("user", "synthetic selected input"))
                .items
            ),
            rebuild_reason="bootstrap",
        )
    return receipt.event, PromptProjectionRepository(database), options


def privacy_reads(statements):
    return [
        sql for sql in statements if sql.startswith("SELECT") and "execution_trace_state" in sql
    ]


async def test_private_snapshot_fresh_and_warm_reads_are_narrow_and_bounded(
    database, tmp_path, monkeypatch
):
    anchor, repository, options = await private_scene(database, tmp_path)
    metrics, phases = {}, ModelPhases()
    metrics_token, phases_token = current_metrics.set(metrics), current_model_phases.set(phases)
    try:
        with capture(database, monkeypatch) as (fresh, sessions):
            plan = await repository.prepare_commit(**options)
        assert len(sessions) == 3  # two reader snapshots and original fresh-source writer
        assert len(privacy_reads(fresh)) == 3  # one scalar per actual transaction
        anchor_reads = [sql for sql in fresh if "FROM chat_events" in sql]
        assert len(anchor_reads) == 2
        assert all(
            sql.startswith("SELECT chat_events.canonical_conversation_id FROM")
            for sql in anchor_reads
        )
        async with database.immediate_session() as writer:
            saved = await plan(writer)
        expected = options["items"][0]["message"]
        assert saved.items()[0]["message"] == expected
        warm_options = dict(
            options,
            expected_epoch=saved.epoch_id,
            expected_revision=saved.revision,
            previous_snapshot=saved,
            previous_item_count=len(saved.items()),
            items=saved.items(),
            rebuild_reason=None,
        )
        # A stored snapshot needs no writer; it can finish with an unrelated WAL writer held.
        async with database.sessions() as held:
            await held.execute(text("BEGIN IMMEDIATE"))
            with capture(database, monkeypatch) as (warm, sessions):
                again = await asyncio.wait_for(repository.prepare_commit(**warm_options), timeout=1)
            await held.rollback()
        assert len(sessions) == 2
        assert len(privacy_reads(warm)) == 2
        assert not any(
            sql.startswith(("INSERT", "UPDATE", "DELETE", "BEGIN IMMEDIATE")) for sql in warm
        )
        async with database.immediate_session() as writer:
            warm_saved = await again(writer)
        assert warm_saved.payload_json == saved.payload_json
        assert anchor.id == options["snapshot_event_id"]
    finally:
        current_metrics.reset(metrics_token)
        current_model_phases.reset(phases_token)
    for name in (
        "projection_snapshot_read",
        "projection_snapshot_publish",
        "projection_sources_read",
    ):
        assert phases.nested[name] > 0
        assert metrics[name + "_seconds"] > 0


@pytest.mark.parametrize("change", ["privacy", "revision", "anchor_rebind"])
async def test_snapshot_writer_rechecks_after_merged_reader_closes(
    database, tmp_path, monkeypatch, change
):
    anchor, repository, options = await private_scene(database, tmp_path)
    original = database.immediate_session
    changed = False

    def interposed():
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def writer():
            nonlocal changed
            if not changed:
                changed = True
                async with original() as rival:
                    if change == "privacy":
                        rival.add(ExecutionTraceStateModel(id=1, privacy_generation=1))
                    elif change == "revision":
                        await rival.execute(
                            update(CanonicalConversationModel)
                            .where(CanonicalConversationModel.id == options["conversation_id"])
                            .values(prompt_source_revision=options["expected_source_revision"] + 1)
                        )
                    else:
                        other = await rival.scalar(
                            select(CanonicalConversationModel.id)
                            .where(CanonicalConversationModel.id != options["conversation_id"])
                            .limit(1)
                        )
                        assert other is not None
                        await rival.execute(
                            update(ChatEventModel)
                            .where(ChatEventModel.id == anchor.id)
                            .values(canonical_conversation_id=other)
                        )
            async with original() as session:
                yield session

        return writer()

    monkeypatch.setattr(database, "immediate_session", interposed)
    with pytest.raises(ProjectionConflict, match=r"snapshot .* changed"):
        await repository.prepare_commit(**options)
    async with database.sessions() as reader:
        assert not (await reader.scalars(select(ContextObservationModel.id))).all()


async def test_guard_reuses_only_same_snapshot_privacy_for_original_and_merged_sources(
    database, tmp_path, monkeypatch
):
    anchor, repository, options = await private_scene(database, tmp_path)
    saved = await repository.commit(**options)
    observation = saved.items()[0]["observation_id"]
    async with database.immediate_session() as writer:
        original = await writer.get(ContextObservationModel, observation)
        writer.add(
            ContextObservationModel(
                id="another-source",
                conversation_id=original.conversation_id,
                generation=original.generation,
                actor_id=original.actor_id,
                read_scope=original.read_scope,
                source_key="synthetic:another",
                version=1,
                payload_json='{"synthetic":"additional"}',
                created_at=original.created_at,
            )
        )
    guard, control = await _guard(database, anchor)
    guard = WorkSourceGuard(
        replace(
            guard.version,
            selected_summary_text="",
            observation_actor_id=options["actor_id"],
            observation_read_scope="main",
            observation_sources=((observation, 1),),
        )
    )
    with capture(database, monkeypatch) as (statements, sessions):
        assert await guard.check(control, observation_sources=(("another-source", 1),))
    assert len(sessions) == 2
    assert len(privacy_reads(statements)) == 2
    assert sum("FROM model_context_observations" in sql for sql in statements) == 2
    assert guard.version.observation_sources == ((observation, 1), ("another-source", 1))
    # The next independent proof must read the new privacy, not reuse the earlier scalar.
    async with database.immediate_session() as writer:
        writer.add(ExecutionTraceStateModel(id=1, privacy_generation=1))
    assert not await guard.check(control)


async def test_same_snapshot_scalar_does_not_hide_privacy_change_before_final_guard(
    database, tmp_path, monkeypatch
):
    from qq_ai_bot.conversation import observations

    anchor, repository, options = await private_scene(database, tmp_path)
    saved = await repository.commit(**options)
    observation = saved.items()[0]["observation_id"]
    original_guard, control = await _guard(database, anchor)
    guard = WorkSourceGuard(
        replace(
            original_guard.version,
            selected_summary_text="",
            observation_actor_id=options["actor_id"],
            observation_read_scope="main",
            observation_sources=((observation, 1),),
        )
    )
    validate = observations.validate_observations
    proofs = []

    async def interposed(session, *args, **kwargs):
        # Real WAL update after this reader already established its snapshot.
        assert kwargs["snapshot_privacy_generation"] == 0
        result = await validate(session, *args, **kwargs)
        proofs.append(result)
        async with database.immediate_session() as writer:
            writer.add(ExecutionTraceStateModel(id=1, privacy_generation=1))
        return result

    monkeypatch.setattr(observations, "validate_observations", interposed)
    assert not await guard.check(control)
    assert proofs == [True]
    assert guard.fingerprint is None
    assert control.session.source_revision == 0
