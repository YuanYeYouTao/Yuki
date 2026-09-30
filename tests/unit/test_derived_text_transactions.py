"""Derived text recounts use a read snapshot, with short fenced commits."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import event, select
from tests.unit.test_conversation_rollup_370 import _policy, _prepare_v2_private

from qq_ai_bot.conversation.canonical_db_models import (
    CanonicalConversationModel,
    CanonicalConversationRollupModel,
)
from qq_ai_bot.conversation.rollup.prompt_accounting import durable_uncovered_event_characters
from qq_ai_bot.domain.audio import AudioTranscript, serialize_transcripts
from qq_ai_bot.domain.messages import InboundMessage, SenderIdentity
from qq_ai_bot.persistence.models import ChatEventModel
from qq_ai_bot.persistence.repository_helpers import _event_record
from qq_ai_bot.persistence.scoped_event_uow import ScopedEventLedgerUnitOfWork


async def _source(database):
    scope = await _prepare_v2_private(database)
    policy = _policy()
    uow = ScopedEventLedgerUnitOfWork(database, config=policy)
    result = await uow.append(
        scope=scope,
        platform_message_id="derived-source",
        sender_user_id="1001",
        direction="inbound",
        content="source",
    )
    return scope, policy, uow, result


async def _cover(database, result):
    async with database.immediate_session() as session:
        conversation = await session.get(
            CanonicalConversationModel, result.event.canonical_conversation_id
        )
        conversation.covered_through_event_id = result.event.id
        conversation.uncovered_event_count = 0
        conversation.uncovered_character_count = 0
        session.add(
            CanonicalConversationRollupModel(
                conversation_id=conversation.id,
                generation=conversation.generation,
                covered_through_event_id=result.event.id,
                summary_text="unrecognized source",
                summary_kind="model",
                source_fingerprint="a" * 64,
                revision=1,
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("late_audio", [False, True])
async def test_conditional_recount_never_scans_history_in_writer(database, late_audio):
    _scope, policy, uow, result = await _source(database)
    if late_audio:
        await _cover(database, result)
    else:
        await uow.set_visual_summary(result.event.id, "long visual description " * 30)
        async with database.immediate_session() as session:
            conversation = await session.get(
                CanonicalConversationModel, result.event.canonical_conversation_id
            )
            conversation.uncovered_character_count = 0
    in_writer = False
    history_reads = 0

    def sql(connection, cursor, statement, parameters, context, many):
        nonlocal in_writer, history_reads
        lowered = statement.lower()
        if lowered.startswith("begin immediate"):
            in_writer = True
        if "from chat_events" in lowered and "chat_events.id >" in lowered:
            assert not in_writer, "recount scanned source history while owning SQLite writer"
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
    assert history_reads == 1
    async with database.sessions() as session:
        conversation = await session.get(
            CanonicalConversationModel, result.event.canonical_conversation_id
        )
        source = _event_record(await session.get(ChatEventModel, result.event.id))
        assert conversation.uncovered_event_count == 1
        assert conversation.uncovered_character_count == durable_uncovered_event_characters(
            source, bot_display_name=policy.bot_display_name, timezone=policy.timezone
        )
        if late_audio:
            assert conversation.covered_through_event_id == conversation.starts_after_event_id
            assert await session.get(CanonicalConversationRollupModel, conversation.id) is None
        else:
            assert uow.metrics.counter_repairs == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["append", "coverage", "generation"])
async def test_late_audio_rechecks_snapshot_before_reset_or_counter_commit(database, change):
    scope, policy, uow, result = await _source(database)
    await _cover(database, result)
    original = uow._prepare_derived_text
    preparations = 0

    async def prepare_and_change(*args, **kwargs):
        nonlocal preparations
        prepared = await original(*args, **kwargs)
        preparations += 1
        if preparations == 1:
            if change == "append":
                # A separate real writer remains free while source work is prepared.
                await uow.append(
                    scope=scope,
                    platform_message_id="new-source",
                    sender_user_id="1001",
                    direction="inbound",
                    content="new concurrent keeper",
                )
            elif change == "coverage":
                async with database.immediate_session() as session:
                    rollup = await session.get(
                        CanonicalConversationRollupModel, result.event.canonical_conversation_id
                    )
                    rollup.revision += 1
                    rollup.summary_text = "a concurrent compacted summary"
            else:
                await uow.append_new_generation_command(
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
        assert preparations == 2
        rows = tuple(
            await session.scalars(
                select(ChatEventModel).where(
                    ChatEventModel.canonical_conversation_id == conversation.id
                )
            )
        )
        assert conversation.uncovered_event_count == len(rows)
        assert conversation.uncovered_character_count == sum(
            durable_uncovered_event_characters(
                _event_record(row),
                bot_display_name=policy.bot_display_name,
                timezone=policy.timezone,
            )
            for row in rows
        )
        assert source.audio_transcript == transcript
        assert await session.get(CanonicalConversationRollupModel, conversation.id) is None
