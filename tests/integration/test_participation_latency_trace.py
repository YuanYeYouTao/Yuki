"""Real local trace recorder, synthetic observer and SQLite; no provider calls."""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

from tests.integration.test_participation_latency_commands import (
    latency_evidence as latency_evidence,
)
from tests.support.event_seed import seed_event
from tests.support.semantic_participation_host_helpers import _observation
from yuki_participation.models import Scope, ScopedEvent, Snapshot, SourceRef

from yuki.domain.conversations import ScopeType
from yuki.execution_trace.recorder import TraceRecorder
from yuki.persistence.diagnostic_writer import DiagnosticWriter
from yuki.persistence.repositories import EventLedgerRepository
from yuki.services.participation_trace import TracedSemanticObserver


async def test_full_trace_writer_does_not_copy_snapshot(database, monkeypatch, latency_evidence):
    event, _ = await seed_event(
        EventLedgerRepository(database),
        bot_user_id="8000",
        origin_key="latency-trace-source",
        scope_type=ScopeType.GROUP,
        group_id="2001",
        sender_user_id="1001",
        direction="inbound",
        content="synthetic source",
    )
    now = time.time()
    scope = Scope(conversation_id=event.canonical_conversation_id, generation=1)
    snapshot = Snapshot(
        scope=scope,
        focus=ScopedEvent(
            scope=scope,
            ref=SourceRef(event_id=f"event:{event.id}", revision=1),
            thread="synthetic-thread",
            author=event.author_person_id,
            target=event.author_person_id,
            text="synthetic snapshot",
            at=now,
        ),
        context=(),
        sequence=1,
        issued_at=now,
    )
    observation = _observation(snapshot)
    observer = SimpleNamespace(evaluate=AsyncMock(return_value=observation))
    # A genuine writer before start has zero available capacity. The normal
    # recorder still owns privacy checks, coverage and drop accounting.
    writer = DiagnosticWriter()
    assert writer.capacity() == 0
    recorder = TraceRecorder(database, writer=writer)
    original_dump = Snapshot.model_dump
    snapshot_copies = 0

    def count_dump(self, *args, **kwargs):
        nonlocal snapshot_copies
        if self is snapshot:
            snapshot_copies += 1
        return original_dump(self, *args, **kwargs)

    monkeypatch.setattr(Snapshot, "model_dump", count_dump)
    result = await TracedSemanticObserver(observer, recorder).evaluate(snapshot)
    latency_evidence.update(
        boundary="real TraceRecorder/DiagnosticWriter/SQLite; synthetic observer; no provider",
        writer_capacity=writer.capacity(),
        snapshot_dump_calls=snapshot_copies,
        recorder_failures=recorder.record_failures,
        writer_dropped=writer.dropped,
        same_observation=result is observation,
    )
    assert result is observation
    observer.evaluate.assert_awaited_once()
    assert recorder.record_failures > 0 and writer.dropped > 0
    assert snapshot_copies == 0, "zero-capacity diagnostics copied the full Snapshot"
