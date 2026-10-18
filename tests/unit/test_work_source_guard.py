"""Readonly source checks close over short lease/revision fences."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, select, update
from sqlalchemy import event as sql_event
from tests.support.semantic_participation_host_helpers import _event_and_route

from yuki.conversation.canonical_db_models import (
    CanonicalConversationModel,
)
from yuki.conversation.projection_revision_schema import EVENT_METADATA_COLUMNS_0082
from yuki.domain.conversations import ConversationScope
from yuki.domain.messages import ChatImage, ChatMessage
from yuki.execution_trace.db_models import ExecutionTraceStateModel
from yuki.identity.canonical_repository import ensure_space
from yuki.persistence.event_repository import ConversationReadVersion, EventLedgerRepository
from yuki.persistence.models import ChatEventModel
from yuki.runtime.work_repository import WorkConflict, WorkRepository
from yuki.runtime.work_source_guard import WorkSourceGuard
from yuki.services.turn_transcript import TurnTranscript, validating_request


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
        repository=repo,
        lease=lease,
        current=None,
        source={},
        session=SimpleNamespace(event_ids=[], source_revision=0),
    )
    return WorkSourceGuard(version), control


async def test_source_scans_finish_before_recheck_and_new_enrichment_remains_allowed(database):
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
    recheck = [index for index, statement in enumerate(sql) if statement == "BEGIN"][1]
    assert any("chat_events" in statement for statement in sql[:recheck])
    assert not any("chat_events" in statement for statement in sql[recheck:])
    assert all(statement.startswith(("BEGIN", "SELECT")) for statement in sql)
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
async def test_fresh_snapshot_recheck_rejects_changes_after_read_snapshot(
    database, monkeypatch, change
):
    source = await _event_and_route(database, EventLedgerRepository(database))
    guard, control = await _guard(database, source)
    original = control.repository._assert_lease_readonly

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

    monkeypatch.setattr(control.repository, "_assert_lease_readonly", between_read_and_write)
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


async def test_unchanged_guard_reuses_sources_but_restoration_reproves_original_rows(database):
    source = await _event_and_route(database, EventLedgerRepository(database))
    guard, control = await _guard(database, source)
    assert await guard.check(control)
    original = guard.snapshot()
    sql = []

    def record(_connection, _cursor, statement, *_args):
        sql.append(statement)

    sql_event.listen(database.engine.sync_engine, "before_cursor_execute", record)
    try:
        assert await guard.check(control)
        assert not any("chat_events" in statement for statement in sql)
        assert any("canonical_conversations" in statement for statement in sql)
        assert guard.snapshot() == original
        sql.clear()
        restored = WorkSourceGuard.restore(original)
        assert await restored.check(control)
        assert any("chat_events" in statement for statement in sql)
    finally:
        sql_event.remove(database.engine.sync_engine, "before_cursor_execute", record)


async def test_new_unselected_enrichment_reuses_the_proved_original_after_revision_recheck(
    database,
):
    ledger = EventLedgerRepository(database)
    source = await _event_and_route(database, ledger)
    guard, control = await _guard(database, source)
    assert await guard.check(control)
    original = guard.fingerprint
    fresh = await _event_and_route(database, ledger, content="not in the original model input")
    async with database.sessions() as session, session.begin():
        await session.execute(
            update(ChatEventModel)
            .where(ChatEventModel.id == fresh.id)
            .values(audio_transcript='{"text":"later unobserved transcript"}')
        )
    assert await guard.check(control)
    assert guard.fingerprint == original
    sql = []

    def record(_connection, _cursor, statement, *_args):
        sql.append(statement)

    sql_event.listen(database.engine.sync_engine, "before_cursor_execute", record)
    try:
        assert await guard.check(control)
    finally:
        sql_event.remove(database.engine.sync_engine, "before_cursor_execute", record)
    assert not any("chat_events" in statement for statement in sql)
    assert guard.fingerprint == original


class _NoTraversalIds(frozenset):
    def __iter__(self):
        raise AssertionError("warm image validation must not traverse the admitted prefix")

    def __or__(self, other):
        raise AssertionError("warm image validation must not copy the admitted prefix")


@pytest.mark.parametrize("selected_image", ["none", "original", "additional"])
async def test_warm_image_validation_uses_membership_without_copying_admitted_ids(
    database, selected_image
):
    ledger = EventLedgerRepository(database)
    source = await _event_and_route(database, ledger)
    guard, control = await _guard(database, source)
    assert await guard.check(control)
    selected = source
    if selected_image == "additional":
        selected = await _event_and_route(database, ledger, content="admitted image source")
        assert await guard.check(control, event_ids=frozenset({selected.id}))
    verified = guard._verified
    assert verified is not None
    visible = _NoTraversalIds(verified.visible_ids)
    eligible = _NoTraversalIds(verified.eligible_images)
    guard._verified = replace(verified, visible_ids=visible, eligible_images=eligible)
    images = (
        (
            ChatImage(
                "data:image/png;base64,synthetic",
                source="history",
                source_event_id=selected.id,
                conversation_id=source.canonical_conversation_id,
                generation=guard.version.generation,
            ),
        )
        if selected_image != "none"
        else ()
    )
    request = TurnTranscript((ChatMessage("user", images=images),)).request()
    with validating_request(request):
        assert await guard.check(control)
    assert guard._verified.visible_ids is visible
    assert guard._verified.eligible_images is eligible


async def test_provisional_guard_reuses_proof_and_can_roll_back_selected_dependencies(database):
    ledger = EventLedgerRepository(database)
    source = await _event_and_route(database, ledger)
    guard, control = await _guard(database, source)
    assert await guard.check(control)
    saved = guard.fork()
    candidate = guard.fork()
    sql = []

    def record(_connection, _cursor, statement, *_args):
        sql.append(statement)

    sql_event.listen(database.engine.sync_engine, "before_cursor_execute", record)
    try:
        assert await candidate.check(control)
    finally:
        sql_event.remove(database.engine.sync_engine, "before_cursor_execute", record)
    assert not any("chat_events" in statement for statement in sql)
    fresh = await _event_and_route(database, ledger, content="new selected dependency")
    assert await candidate.check(control, event_ids=frozenset({fresh.id}))
    assert fresh.id not in guard.additional_events
    guard.adopt(candidate)
    assert fresh.id in guard.additional_events
    guard.adopt(saved)
    assert fresh.id not in guard.additional_events
