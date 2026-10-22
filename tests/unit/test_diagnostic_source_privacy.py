"""Removing diagnostic preflight reads preserves the conditional source proofs."""

import asyncio
import threading
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import event, func, select
from tests.support.social_identity_cases import social_env

from yuki.execution_trace import recorder as recorder_module
from yuki.execution_trace.db_models import ExecutionTraceEntryModel, ExecutionTraceStateModel
from yuki.execution_trace.recorder import (
    TraceCoverage,
    TraceRecorder,
    TraceScope,
    current_trace,
    trace_span,
)
from yuki.model_runtime.db_models import ModelInvocationModel
from yuki.model_runtime.models import ModelTask
from yuki.model_runtime.repository import ModelInvocationRepository
from yuki.persistence.diagnostic_writer import DiagnosticWriter
from yuki.persistence.models import ChatEventModel
from yuki.social.db_models import SocialOperationModel


def scope(recorder, *, conversation=None, source=None, generation=0):
    return TraceScope(
        recorder,
        TraceCoverage(generation),
        "original-turn",
        "original-operation",
        None,
        conversation,
        "original-execution",
        "user_message",
        source,
    )


async def model_record(repository, *, conversation=None):
    return await repository.record(
        task=ModelTask.CHAT_AGENT,
        profile_id="original-profile",
        provider="offline",
        model="original-model",
        success=True,
        prompt_tokens=100,
        completion_tokens=5,
        total_tokens=105,
        cached_prompt_tokens=80,
        latency_seconds=0.1,
        error_category=None,
        canonical_conversation_id=conversation,
    )


async def test_diagnostic_source_proof_uses_no_per_record_read_session(database, tmp_path):
    env = await social_env(database, tmp_path)
    async with database.read_session() as reader:
        source = await reader.scalar(select(ChatEventModel))
    writer = DiagnosticWriter()
    recorder = TraceRecorder(database, writer=writer)
    repository = ModelInvocationRepository(database, writer=writer)
    original = scope(recorder, conversation=env.context.conversation_id, source=source.id)
    statements = []

    def observe(_conn, _cursor, sql, _parameters, _context, _many):
        statements.append(sql)

    event.listen(database.engine.sync_engine, "before_cursor_execute", observe)
    await writer.start()
    token = current_trace.set(original)
    try:
        await recorder.append(original, "provider_response", {"body": "original"})
        await model_record(repository, conversation=env.context.conversation_id)
    finally:
        current_trace.reset(token)
    await writer.close()
    event.remove(database.engine.sync_engine, "before_cursor_execute", observe)
    # Both write-time privacy/source checks remain inside their INSERT SELECT.
    assert not any(sql.lstrip().upper().startswith("SELECT") for sql in statements)
    assert writer.committed == 2
    assert original.coverage.failures == 0
    async with database.read_session() as reader:
        row = await reader.scalar(select(ModelInvocationModel))
        assert row.cached_prompt_tokens == 80
        assert row.total_tokens == 105


async def test_erasure_before_writer_rejects_old_trace_and_model_without_recapturing(
    database, monkeypatch, caplog
):
    writer = DiagnosticWriter()
    recorder = TraceRecorder(database, writer=writer)
    repository = ModelInvocationRepository(database, writer=writer)
    original = scope(recorder)
    prepare = recorder._prepare_write

    async def erase_after_encode(*args):
        prepared = await prepare(*args)
        async with database.immediate_session() as session:
            session.add(ExecutionTraceStateModel(id=1, privacy_generation=1))
        return prepared

    monkeypatch.setattr(recorder, "_prepare_write", erase_after_encode)
    await writer.start()
    token = current_trace.set(original)
    try:
        await recorder.append(original, "old", {"body": "old"})
        await model_record(repository)
    finally:
        current_trace.reset(token)
    await writer.drain()
    assert writer.failures == 2
    assert writer.committed == 0
    assert original.coverage.failures == 1
    assert "diagnostic_discarded kind=old" in caplog.text
    assert "diagnostic_discarded kind=model_invocation" in caplog.text
    monkeypatch.setattr(recorder, "_prepare_write", prepare)
    await recorder.append(scope(recorder, generation=1), "fresh", {})
    await writer.close()
    async with database.read_session() as reader:
        assert (await reader.scalars(select(ExecutionTraceEntryModel.kind))).all() == ["fresh"]
        assert await reader.scalar(select(func.count(ModelInvocationModel.id))) == 0


@pytest.mark.parametrize("change", ["source_owner_mismatch", "wrong_kind", "missing_event"])
async def test_original_conversation_event_source_is_checked_at_write_time(
    database, tmp_path, change
):
    env = await social_env(database, tmp_path)
    async with database.read_session() as reader:
        source = await reader.scalar(select(ChatEventModel))
    writer = DiagnosticWriter()
    recorder = TraceRecorder(database, writer=writer)
    original = scope(recorder, conversation=source.canonical_conversation_id, source=source.id)
    if change == "source_owner_mismatch":
        original = replace(original, conversation_id=None)
    elif change == "wrong_kind":
        original = replace(original, conversation_id=env.person)
    elif change == "missing_event":
        original = replace(original, source_event_id=source.id + 1000)
    await writer.start()
    await recorder.append(original, "source", {})
    await writer.close()
    assert original.coverage.failures == 1
    assert writer.failures == 1
    async with database.read_session() as reader:
        assert await reader.scalar(select(func.count(ExecutionTraceEntryModel.id))) == 0


@pytest.mark.parametrize("valid", [False, True])
async def test_delivery_diagnostic_requires_original_confirmed_receipt_and_event(
    database, tmp_path, valid
):
    env = await social_env(database, tmp_path)
    sent = await env.service.execute("send", {"text": "confirmed"}, env.context)
    async with database.read_session() as reader:
        source = await reader.scalar(
            select(ChatEventModel).where(ChatEventModel.direction == "inbound")
        )
        receipt = await reader.scalar(select(SocialOperationModel))
    recorder = TraceRecorder(database)
    original = scope(recorder, conversation=source.canonical_conversation_id, source=source.id)
    await recorder.append(
        original,
        "social_delivery",
        {"event_id": sent["event_id"]},
        delivery=(receipt.id if valid else "missing-receipt", sent["event_id"]),
    )
    async with database.read_session() as reader:
        rows = (await reader.scalars(select(ExecutionTraceEntryModel))).all()
        assert len(rows) == int(valid)
        assert await reader.scalar(select(SocialOperationModel.status)) == "succeeded"
    assert original.coverage.failures == int(not valid)
    if valid:
        assert rows[0].source_event_id == source.id
        assert rows[0].delivered_event_id == sent["event_id"]


async def test_shutdown_joins_actual_encoder_before_releasing_original_reservation(
    database, monkeypatch
):
    entered = threading.Event()
    release = threading.Event()
    encode = recorder_module._encode_payload_timed

    def held_encoder(*args):
        entered.set()
        release.wait()
        return encode(*args)

    monkeypatch.setattr(recorder_module, "_encode_payload_timed", held_encoder)
    writer = DiagnosticWriter()
    recorder = TraceRecorder(database, writer=writer)
    original = scope(recorder)
    await writer.start()
    await recorder.append(original, "cancelled", {"body": "held"})
    await asyncio.to_thread(entered.wait)
    closing = asyncio.create_task(writer.close(drain_seconds=0))
    try:
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not closing.done()
        assert (await writer.health())["bytes"] > 0
    finally:
        release.set()
        await closing
    assert (await writer.health())["bytes"] == 0
    assert writer.dropped == 1
    assert original.coverage.failures == 1


async def test_only_consumed_live_families_publish_entry_exit_hints(database, monkeypatch):
    recorder = TraceRecorder(database)
    monkeypatch.setattr(recorder, "coverage", AsyncMock(return_value=TraceCoverage(0)))
    monkeypatch.setattr(recorder_module, "record_trace", AsyncMock())
    # A plain counter avoids tying the assertion to database after_commit hints.
    count = []
    hints = SimpleNamespace(publish=lambda: count.append(True))
    monkeypatch.setattr(database, "commit_hints", hints)
    async with trace_span("chat_processing", {}, recorder=recorder, conversation_id="original"):
        assert len(recorder.live_spans("original")) == 1
        async with trace_span("model", {}, record_start=False):
            assert len(recorder._live_spans) == 1
            assert len(recorder.live_spans("original")) == 1
        assert len(count) == 1
        async with trace_span("turn", {}):
            assert len(recorder.live_spans("original")) == 2
        assert len(count) == 3
    assert len(count) == 4
    assert recorder._live_spans == {}
    assert current_trace.get() is None
