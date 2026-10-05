"""Consumers keep original source checks narrow; producers do not query sources."""

import asyncio
import logging
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import delete, event, insert, select, text, update

from qq_ai_bot.conversation.correlation import (
    CANONICAL_KIND_MISMATCH,
    MISSING_CANONICAL_CONVERSATION,
)
from qq_ai_bot.conversation.rollup.models import RollupPolicyConfig
from qq_ai_bot.domain.conversations import ConversationScope
from qq_ai_bot.execution_trace import recorder as recording
from qq_ai_bot.execution_trace.db_models import ExecutionTraceEntryModel, ExecutionTraceStateModel
from qq_ai_bot.execution_trace.payload import encode_payload
from qq_ai_bot.execution_trace.recorder import TraceCoverage, TraceRecorder, TraceScope
from qq_ai_bot.identity.canonical_repository import ensure_space
from qq_ai_bot.identity.errors import CanonicalIdentityError
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.diagnostic_writer import DiagnosticWriter
from qq_ai_bot.persistence.models import ChatEventModel
from qq_ai_bot.persistence.scoped_event_uow import ScopedEventLedgerUnitOfWork
from tests.support.social_identity_cases import social_env


class CapturedWrites(DiagnosticWriter):
    def __init__(self):
        super().__init__()
        self.writes = []

    def capacity(self):
        return 32 * 1024 * 1024

    def submit(self, kind, size, commit):
        self.writes.append((kind, size, commit))
        return True


def scope(recorder, conversation_id=None, event_id=None, generation=0):
    return TraceScope(
        recorder,
        TraceCoverage(generation),
        "trace-test-turn",
        str(uuid4()),
        None,
        conversation_id,
        None,
        "user_message",
        event_id,
    )


@pytest.fixture
async def source(database, tmp_path):
    env = await social_env(database, tmp_path)
    async with database.sessions() as session:
        event_id = await session.scalar(select(ChatEventModel.id).order_by(ChatEventModel.id))
    async with database.immediate_session() as session:
        await ensure_space(session, "20002")
    writer = ScopedEventLedgerUnitOfWork(database, config=RollupPolicyConfig())
    await writer.append(
        scope=ConversationScope.group(env.bot.self_id, "20002"),
        platform_message_id="synthetic-foreign-source",
        sender_user_id="10001",
        direction="inbound",
        content="synthetic",
    )
    async with database.sessions() as session:
        env.foreign_event_id = await session.scalar(
            select(ChatEventModel.id).where(
                ChatEventModel.canonical_conversation_id != env.context.conversation_id
            )
        )
    return env, event_id


async def test_source_checks_preserve_identity_and_error_priority(database, source):
    env, event_id = source
    cases = [
        (env.context.conversation_id, event_id, None),
        (env.context.conversation_id, None, None),
        (None, None, None),
        (None, event_id, "invalid_trace_source_event"),
        (env.context.conversation_id, env.foreign_event_id, "invalid_trace_source_event"),
        (env.context.conversation_id, 987654321, "invalid_trace_source_event"),
        (str(uuid4()), event_id, MISSING_CANONICAL_CONVERSATION),
        (env.person, event_id, CANONICAL_KIND_MISMATCH),
        (env.presence, event_id, CANONICAL_KIND_MISMATCH),
        (env.space, event_id, CANONICAL_KIND_MISMATCH),
        # A missing source must not replace the original kind/missing category.
        (env.person, 987654321, CANONICAL_KIND_MISMATCH),
        (str(uuid4()), 987654321, MISSING_CANONICAL_CONVERSATION),
        ("", event_id, MISSING_CANONICAL_CONVERSATION),
        ("  ", None, MISSING_CANONICAL_CONVERSATION),
        (f" {env.context.conversation_id} ", None, None),
        (f" {env.context.conversation_id} ", event_id, "invalid_trace_source_event"),
    ]
    for conversation_id, source_id, expected in cases:
        async with database.sessions() as session:
            if expected is None:
                await recording._require_trace_source(session, conversation_id, source_id)
            else:
                with pytest.raises((CanonicalIdentityError, ValueError)) as failure:
                    await recording._require_trace_source(session, conversation_id, source_id)
                actual = (
                    failure.value.category
                    if isinstance(failure.value, CanonicalIdentityError)
                    else str(failure.value)
                )
                assert actual == expected


async def test_append_source_is_one_narrow_select_without_body_reads(database, source):
    env, event_id = source
    writer = CapturedWrites()
    recorder = TraceRecorder(database, writer=writer)
    current = scope(recorder, env.context.conversation_id, event_id)
    selected = []

    def capture(_connection, _cursor, statement, _parameters, *_rest):
        if statement.lstrip().upper().startswith("SELECT"):
            selected.append(statement)

    event.listen(database.engine.sync_engine, "before_cursor_execute", capture)
    try:
        await recorder.append(current, "model_start", {"request": "synthetic"})
        assert not selected
        await writer.writes[0][2]()
    finally:
        event.remove(database.engine.sync_engine, "before_cursor_execute", capture)
    assert len(selected) == 1
    assert "chat_events.id" in selected[0]
    assert "chat_events.canonical_conversation_id" in selected[0]
    for field in (
        "content",
        "segments_json",
        "external_payload_json",
        "visual_summary",
        "audio_transcript",
    ):
        assert f"chat_events.{field}" not in selected[0]
    assert len(writer.writes) == 1 and current.coverage.failures == 0
    async with database.sessions() as session:
        stored = await session.scalar(select(ExecutionTraceEntryModel))
    assert stored.source_event_id == event_id
    assert stored.conversation_id == env.context.conversation_id


async def test_nullable_legacy_source_is_distinct_from_missing_event(tmp_path):
    # The current schema disallows NULL. This real SQLite legacy shape verifies
    # the recorder's existing (None, NULL) contract without weakening that schema.
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'legacy.db').as_posix()}")
    try:
        async with database.engine.begin() as connection:
            for table in ("presences", "persons", "spaces", "canonical_conversations"):
                await connection.execute(text(f"CREATE TABLE {table} (id TEXT PRIMARY KEY)"))
            await connection.execute(
                text(
                    "CREATE TABLE chat_events "
                    "(id INTEGER PRIMARY KEY, canonical_conversation_id TEXT)"
                )
            )
            await connection.execute(text("INSERT INTO chat_events VALUES (1, NULL), (2, 'other')"))
        for event_id, accepted in ((None, True), (1, True), (2, False), (3, False)):
            async with database.sessions() as session:
                if accepted:
                    await recording._require_trace_source(session, None, event_id)
                else:
                    with pytest.raises(ValueError, match="invalid_trace_source_event"):
                        await recording._require_trace_source(session, None, event_id)
    finally:
        await database.close()


async def test_source_rechecks_after_physical_deletion_and_privacy_fences_queued_write(
    database,
    source,
):
    env, event_id = source
    async with database.immediate_session() as session:
        await session.execute(insert(ExecutionTraceStateModel).values(id=1, privacy_generation=0))
    writer = DiagnosticWriter()
    await writer.start()
    recorder = TraceRecorder(database, writer=writer)
    current = scope(recorder, env.context.conversation_id, event_id)
    try:
        async with database.immediate_session() as erasure:
            await erasure.execute(delete(ChatEventModel).where(ChatEventModel.id == event_id))
            await erasure.execute(update(ExecutionTraceStateModel).values(privacy_generation=1))
            # WAL readers see the committed source while erasure holds the writer.
            # Preparing/enqueueing this trace never waits for the diagnostic DML.
            await asyncio.wait_for(recorder.append(current, "model_start", {}), timeout=0.5)
            assert current.coverage.failures == 0
        await writer.drain()
        async with database.sessions() as session:
            assert await session.get(ChatEventModel, event_id) is None
            assert await session.scalar(select(ExecutionTraceEntryModel.id)) is None
        assert current.coverage.failures == 1
        fresh = scope(recorder, env.context.conversation_id, event_id, generation=1)
        await recorder.append(fresh, "model_start", {})
        await writer.drain()
        assert fresh.coverage.failures == 1
        assert writer.committed + writer.failures == 2  # both callbacks consumed once
    finally:
        await writer.close()


async def test_slow_preparation_logs_only_numbers_and_original_correlation(
    database,
    source,
    monkeypatch,
    caplog,
):
    env, event_id = source
    writer = CapturedWrites()
    recorder = TraceRecorder(database, writer=writer)
    current = scope(recorder, env.context.conversation_id, event_id)
    clock = [10.0]
    monkeypatch.setattr(recording, "time", SimpleNamespace(perf_counter=lambda: clock[0]))

    def encode(value, limit):
        encoded = encode_payload(value, limit)
        clock[0] += 0.4
        return encoded

    async def to_thread(operation, *args):
        clock[0] += 0.2
        result = operation(*args)
        clock[0] += 0.1
        return result

    require_source = recording._require_trace_source

    async def validate(*args):
        await require_source(*args)
        clock[0] += 0.4

    monkeypatch.setattr(recording, "encode_payload", encode)
    monkeypatch.setattr(recording.asyncio, "to_thread", to_thread)
    monkeypatch.setattr(recording, "_require_trace_source", validate)
    with caplog.at_level(logging.WARNING, logger=recording.__name__):
        await recorder.append(current, "model_start", {"content": "private-body-marker"})
        assert not caplog.records
        await writer.writes[-1][2]()
    records = [
        r for r in caplog.records if r.getMessage().startswith("execution_trace_slow_prepare")
    ]
    assert len(records) == 1
    message = records[0].getMessage()
    assert "kind=model_start turn_id=trace-test-turn" in message
    assert "prepare_seconds=1.100000" in message
    assert "encode_call_inclusive_seconds=0.700000" in message
    assert "encode_execution_seconds=0.400000" in message
    assert "source_validation_seconds=0.400000" in message
    assert "private-body-marker" not in message
    assert env.context.conversation_id not in message
    assert len(writer.writes) == 1 and current.coverage.failures == 0


def test_slow_preparation_threshold_has_no_diagnostic_side_effect(caplog):
    with caplog.at_level(logging.WARNING, logger=recording.__name__):
        recording._log_slow_preparation("model_start", "trace-test-turn", 0.999, 0.5, 0.2, 0.499)
        assert not caplog.records
        recording._log_slow_preparation("model_start", "trace-test-turn", 1.0, 0.5, 0.2, 0.5)
    assert len(caplog.records) == 1


async def test_consumer_encode_cancellation_does_not_emit_a_second_diagnostic(
    database,
    monkeypatch,
    caplog,
):
    writer = CapturedWrites()
    recorder = TraceRecorder(database, writer=writer)
    current = scope(recorder)

    async def cancelled(*_args):
        raise asyncio.CancelledError()

    monkeypatch.setattr(recording.asyncio, "to_thread", cancelled)
    with caplog.at_level(logging.WARNING, logger=recording.__name__):
        await recorder.append(current, "model_start", {})
        with pytest.raises(asyncio.CancelledError):
            await writer.writes[0][2]()
    assert not caplog.records
    assert len(writer.writes) == 1 and recorder.record_failures == 1
