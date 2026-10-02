"""Readonly source checks close over short lease/revision fences."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, select, update
from sqlalchemy import event as sql_event
from tests.unit.test_semantic_participation_host import _event_and_route

from qq_ai_bot.conversation.canonical_db_models import (
    CanonicalConversationModel,
    CanonicalConversationRollupEmergencyOverlayModel,
    CanonicalConversationRollupModel,
)
from qq_ai_bot.conversation.projection_revision_schema import EVENT_METADATA_COLUMNS_0082
from qq_ai_bot.domain.conversations import ConversationScope
from qq_ai_bot.execution_trace.db_models import ExecutionTraceStateModel
from qq_ai_bot.identity.canonical_repository import ensure_space
from qq_ai_bot.persistence.event_repository import ConversationReadVersion, EventLedgerRepository
from qq_ai_bot.persistence.models import ChatEventModel
from qq_ai_bot.runtime.work_repository import WorkConflict, WorkRepository
from qq_ai_bot.runtime.work_source_guard import WorkSourceGuard


async def _guard(database, event):
    repo = WorkRepository(database)
    lease = await repo.acquire(event.canonical_conversation_id, 1)
    async with database.sessions() as session:
        row = await session.get(CanonicalConversationModel, event.canonical_conversation_id)
        version = ConversationReadVersion(
            ConversationScope.group("8000", "2001"),
            row.id,
            row.generation,
            row.starts_after_event_id,
            row.prompt_source_revision,
            (event.id,),
        )
    control = SimpleNamespace(
        repository=repo, lease=lease, session=SimpleNamespace(event_ids=[], source_revision=0)
    )
    return WorkSourceGuard(version), control


async def test_source_scans_finish_before_writer_and_new_enrichment_remains_allowed(database):
    ledger = EventLedgerRepository(database)
    initial = await _event_and_route(database, ledger)
    guard, control = await _guard(database, initial)
    sql = []

    def record(_connection, _cursor, statement, *_args):
        sql.append(statement)

    sql_event.listen(database.engine.sync_engine, "before_cursor_execute", record)
    try:
        assert await guard.check(control)
    finally:
        sql_event.remove(database.engine.sync_engine, "before_cursor_execute", record)
    writer = next(index for index, statement in enumerate(sql) if statement.startswith("UPDATE"))
    assert any("chat_events" in statement for statement in sql[:writer])
    assert not any(
        "chat_events" in statement or "rollups" in statement for statement in sql[writer:]
    )
    assert sql[0] == "BEGIN"
    additional = await _event_and_route(database, ledger, content="new event")
    control.session.event_ids.append(additional.id)
    async with database.sessions() as session, session.begin():
        await session.execute(
            update(ChatEventModel)
            .where(ChatEventModel.id == additional.id)
            .values(audio_transcript='{"text":"new transcript"}')
        )
    assert await guard.check(control)
    prior = dict(guard.additional_events)
    async with database.sessions() as session, session.begin():
        await session.execute(
            update(ChatEventModel)
            .where(ChatEventModel.id == additional.id)
            .values(sender_nickname="changed admitted author metadata")
        )
    assert not await guard.check(control)
    assert guard.additional_events == prior


@pytest.mark.parametrize(
    "change", ["metadata", "privacy", "privacy_counter", "cancel", "reset", "owner"]
)
async def test_writer_recheck_rejects_changes_after_read_snapshot(database, monkeypatch, change):
    source = await _event_and_route(database, EventLedgerRepository(database))
    guard, control = await _guard(database, source)
    original = control.repository._assert_lease

    async def between_read_and_write(session, lease):
        if change == "cancel":
            await control.repository.cancel(source.canonical_conversation_id, generation=1)
        else:
            async with database.sessions() as other, other.begin():
                if change == "metadata":
                    await other.execute(
                        update(ChatEventModel)
                        .where(ChatEventModel.id == source.id)
                        .values(observed_at=datetime.now(UTC) + timedelta(seconds=1))
                    )
                elif change == "privacy":
                    await other.execute(
                        delete(ChatEventModel).where(ChatEventModel.id == source.id)
                    )
                elif change == "privacy_counter":
                    state = await other.get(ExecutionTraceStateModel, 1)
                    if state is None:
                        other.add(ExecutionTraceStateModel(id=1, privacy_generation=1))
                    else:
                        state.privacy_generation += 1
                elif change == "owner":
                    space = await ensure_space(other, "2990", name="other owner")
                    await other.execute(
                        update(CanonicalConversationModel)
                        .where(CanonicalConversationModel.id == source.canonical_conversation_id)
                        .values(space_id=space)
                    )
                else:
                    await other.execute(
                        update(CanonicalConversationModel)
                        .where(CanonicalConversationModel.id == source.canonical_conversation_id)
                        .values(generation=2)
                    )
        await original(session, lease)

    monkeypatch.setattr(control.repository, "_assert_lease", between_read_and_write)
    if change == "cancel":
        with pytest.raises(WorkConflict, match="work_activation_obsolete"):
            await guard.check(control)
    else:
        assert not await guard.check(control)
    assert guard.fingerprint is None
    assert guard.additional_events == {}
    assert control.session.source_revision == 0


async def test_selected_identity_mutation_and_cross_conversation_extra_are_rejected(database):
    ledger = EventLedgerRepository(database)
    source = await _event_and_route(database, ledger)
    guard, control = await _guard(database, source)
    assert await guard.check(control)
    target = await _event_and_route(database, ledger, group="2002")
    control.session.event_ids = [target.id]
    assert not await guard.check(control)

    control.session.event_ids = []
    async with database.sessions() as session, session.begin():
        await session.execute(
            update(ChatEventModel)
            .where(ChatEventModel.id == source.id)
            .values(sender_nickname="changed selected identity")
        )
    assert not await guard.check(control)


async def test_owner_mutation_before_first_check_rejects_stale_read_version(database):
    source = await _event_and_route(database, EventLedgerRepository(database))
    guard, control = await _guard(database, source)
    async with database.sessions() as session, session.begin():
        space = await ensure_space(session, "2990", name="other owner")
        await session.execute(
            update(CanonicalConversationModel)
            .where(CanonicalConversationModel.id == source.canonical_conversation_id)
            .values(space_id=space)
        )
    assert not await guard.check(control)
    assert guard.fingerprint is None


@pytest.mark.parametrize(
    "model", [CanonicalConversationRollupModel, CanonicalConversationRollupEmergencyOverlayModel]
)
async def test_moving_rollup_invalidates_old_owner_during_read_write_gap(
    database, monkeypatch, model
):
    ledger = EventLedgerRepository(database)
    source = await _event_and_route(database, ledger)
    target = await _event_and_route(database, ledger, group="2002")
    now = datetime.now(UTC)
    values = dict(
        conversation_id=source.canonical_conversation_id,
        generation=1,
        covered_through_event_id=0,
        summary_text="original snapshot",
        source_fingerprint="a" * 64,
        revision=1,
        created_at=now,
        updated_at=now,
    )
    values.update(summary_kind="model" if model is CanonicalConversationRollupModel else None)
    if model is CanonicalConversationRollupEmergencyOverlayModel:
        values.pop("summary_kind")
        values["base_semantic_revision"] = 0
    async with database.sessions() as session, session.begin():
        session.add(model(**values))
    guard, control = await _guard(database, source)
    original = control.repository._assert_lease

    async def move(session, lease):
        async with database.sessions() as other, other.begin():
            await other.execute(
                update(model)
                .where(model.conversation_id == source.canonical_conversation_id)
                .values(conversation_id=target.canonical_conversation_id)
            )
        await original(session, lease)

    monkeypatch.setattr(control.repository, "_assert_lease", move)
    assert not await guard.check(control)
    assert guard.fingerprint is None


async def test_metadata_trigger_closure_and_noop_preserve_revision(database):
    columns = set(ChatEventModel.__table__.columns.keys())
    content_columns = {
        "content",
        "segments_json",
        "visual_summary",
        "external_payload_json",
        "suppression_status",
        "canonical_conversation_id",
        "audio_transcript",
    }
    assert columns == set(EVENT_METADATA_COLUMNS_0082) | content_columns
    source = await _event_and_route(database, EventLedgerRepository(database))
    async with database.sessions() as session:
        before = await session.scalar(
            select(CanonicalConversationModel.prompt_source_revision).where(
                CanonicalConversationModel.id == source.canonical_conversation_id
            )
        )
    async with database.sessions() as session, session.begin():
        await session.execute(
            update(ChatEventModel)
            .where(ChatEventModel.id == source.id)
            .values(sender_nickname=ChatEventModel.sender_nickname)
        )
    async with database.sessions() as session:
        after = await session.scalar(
            select(CanonicalConversationModel.prompt_source_revision).where(
                CanonicalConversationModel.id == source.canonical_conversation_id
            )
        )
    assert before == after


async def test_invalid_semantic_coverage_is_not_an_empty_summary_read_set(database):
    source = await _event_and_route(database, EventLedgerRepository(database))
    now = datetime.now(UTC)
    async with database.sessions() as writer, writer.begin():
        writer.add(
            CanonicalConversationRollupModel(
                conversation_id=source.canonical_conversation_id,
                generation=1,
                covered_through_event_id=source.id + 100,
                summary_text="invalid coverage but nonempty compiler summary",
                summary_kind="model",
                source_fingerprint="a" * 64,
                revision=1,
                created_at=now,
                updated_at=now,
            )
        )
    guard, control = await _guard(database, source)
    assert not await guard.check(control)
    assert guard.fingerprint is None
