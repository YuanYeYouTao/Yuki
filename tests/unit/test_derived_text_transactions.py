"""Derived text uses original source snapshots and short fenced commits."""

import pytest
from sqlalchemy import event
from tests.support.event_seed import seed_scoped_event
from tests.support.public_history import _prepare_v2_private, _publish_checkpoint

from yuki.conversation.canonical_db_models import (
    CanonicalConversationModel,
)
from yuki.conversation.projection_models import PromptProjectionModel
from yuki.domain.audio import AudioTranscript, serialize_transcripts
from yuki.domain.messages import InboundMessage, SenderIdentity
from yuki.persistence.models import ChatEventModel
from yuki.persistence.scoped_event_uow import ScopedEventLedgerUnitOfWork


async def _source(database):
    scope = await _prepare_v2_private(database)
    uow = ScopedEventLedgerUnitOfWork(database)
    result = await seed_scoped_event(
        uow,
        scope=scope,
        platform_message_id="derived-source",
        sender_user_id="1001",
        direction="inbound",
        content="source",
    )
    return scope, uow, result


async def _cover(database, result):
    await _publish_checkpoint(database, result.scope.id, result.event.id)


@pytest.mark.asyncio
@pytest.mark.parametrize("late_audio", [False, True])
async def test_derived_text_never_scans_history_in_writer(database, late_audio):
    _scope, uow, result = await _source(database)
    if late_audio:
        await _cover(database, result)
    else:
        await uow.set_visual_summary(result.event.id, "long visual description " * 30)
    in_writer = False
    history_reads = 0

    def sql(connection, cursor, statement, parameters, context, many):
        nonlocal in_writer, history_reads
        lowered = statement.lower()
        if lowered.startswith("begin immediate"):
            in_writer = True
        if "from chat_events" in lowered and "chat_events.id >" in lowered:
            assert not in_writer, "source history scanned while owning SQLite writer"
            history_reads += 1

    def ended(connection):
        nonlocal in_writer
        in_writer = False

    engine = database.engine.sync_engine
    event.listen(engine, "before_cursor_execute", sql)
    event.listen(engine, "commit", ended)
    event.listen(engine, "rollback", ended)
    try:
        if late_audio:
            transcript = serialize_transcripts((AudioTranscript("current", 0, "new speech"),))
            assert await uow.set_audio_transcript(result.event.id, transcript, generation=1)
        else:
            assert await uow.set_visual_summary(result.event.id, "x")
    finally:
        event.remove(engine, "before_cursor_execute", sql)
        event.remove(engine, "commit", ended)
        event.remove(engine, "rollback", ended)
    assert history_reads == 0
    async with database.sessions() as session:
        source = await session.get(ChatEventModel, result.event.id)
        if late_audio:
            checkpoint = await session.get(PromptProjectionModel, result.scope.id)
            assert checkpoint.invalidated_reason == "source_changed"
            assert source.audio_transcript == transcript
        else:
            assert source.visual_summary == "x"


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["append", "append_many", "generation"])
async def test_late_audio_rechecks_original_source_before_commit(database, change):
    scope, uow, result = await _source(database)
    await _cover(database, result)
    original = uow._prepare_derived_text
    preparations = 0

    async def prepare_and_change(*args, **kwargs):
        nonlocal preparations
        prepared = await original(*args, **kwargs)
        preparations += 1
        if preparations == 1 or (change == "append_many" and preparations <= 5):
            if change in {"append", "append_many"}:
                # A separate real writer remains free while source work is prepared.
                await seed_scoped_event(
                    uow,
                    scope=scope,
                    platform_message_id=f"new-source-{preparations}",
                    sender_user_id="1001",
                    direction="inbound",
                    content="new concurrent keeper",
                )
            else:
                from tests.support.canonical_ingress import append_new_generation

                await append_new_generation(
                    uow,
                    scope=scope,
                    inbound=InboundMessage(
                        message_id="reset-source",
                        event_type="message:test",
                        sender=SenderIdentity("1001"),
                        scope_type=scope.scope_type,
                        bot_user_id=scope.bot_user_id,
                        text="/ai new",
                    ),
                )
        return prepared

    uow._prepare_derived_text = prepare_and_change
    transcript = serialize_transcripts((AudioTranscript("current", 0, "new speech"),))
    assert await uow.set_audio_transcript(result.event.id, transcript, generation=1) is (
        change != "generation"
    )
    async with database.sessions() as session:
        conversation = await session.get(
            CanonicalConversationModel, result.event.canonical_conversation_id
        )
        source = await session.get(ChatEventModel, result.event.id)
        if change == "generation":
            assert conversation.generation == 2 and not source.audio_transcript
            return
        assert preparations == (6 if change == "append_many" else 2)
        assert source.audio_transcript == transcript
