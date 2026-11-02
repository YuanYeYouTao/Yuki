"""Offline trace field-graph boundary, copying and privacy with real SQLite."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from typing import ClassVar
from unittest.mock import AsyncMock

import pytest
from pydantic import BaseModel, model_serializer
from sqlalchemy import select
from tests.integration.test_participation_latency_commands import (
    latency_evidence as latency_evidence,
)
from tests.support.event_seed import seed_event
from tests.support.semantic_participation_host_helpers import _observation
from tests.unit.test_diagnostic_source_privacy import scope as diagnostic_scope
from tests.unit.test_execution_trace_reduction import records
from yuki_participation.models import Scope, ScopedEvent, Snapshot, SourceRef

from yuki.domain.conversations import ScopeType
from yuki.execution_trace.db_models import ExecutionTraceEntryModel, ExecutionTraceStateModel
from yuki.execution_trace.payload import ModelFieldSnapshot, PayloadCapacityError, freeze_payload
from yuki.execution_trace.recorder import TraceRecorder
from yuki.persistence.diagnostic_writer import DiagnosticWriter
from yuki.persistence.repositories import EventLedgerRepository
from yuki.services.participation_trace import TracedSemanticObserver


class ExpandingModel(BaseModel):
    data: dict[str, list[str]]
    serializer_calls: ClassVar[int] = 0

    @model_serializer(mode="plain")
    def expand(self):
        type(self).serializer_calls += 1
        return {"expanded": "x" * (8 * 1024 * 1024)}


class ArbitraryFields(BaseModel):
    data: object


async def make_snapshot(database):
    event, _ = await seed_event(
        EventLedgerRepository(database),
        bot_user_id="8000",
        origin_key="field-snapshot-source",
        scope_type=ScopeType.GROUP,
        group_id="2001",
        sender_user_id="1001",
        direction="inbound",
        content="synthetic source",
    )
    now = time.time()
    key = Scope(conversation_id=event.canonical_conversation_id, generation=1)
    return Snapshot(
        scope=key,
        focus=ScopedEvent(
            scope=key,
            ref=SourceRef(event_id=f"event:{event.id}", revision=1),
            thread="synthetic",
            author=event.author_person_id,
            target=event.author_person_id,
            text="synthetic source",
            at=now,
        ),
        context=(),
        sequence=1,
        issued_at=now,
    )


def test_explicit_field_snapshot_rejects_implicit_models_and_never_executes_serializer(
    latency_evidence,
):
    ExpandingModel.serializer_calls = 0
    model = ExpandingModel(data={"nested": ["original"]})
    with pytest.raises(TypeError, match="diagnostic_payload_type"):
        freeze_payload(model, 32 * 1024 * 1024)
    copied, _ = freeze_payload(ModelFieldSnapshot(model), 32 * 1024 * 1024)
    model.data["nested"].append("later mutation")
    assert copied == {"data": {"nested": ["original"]}}
    with pytest.raises(PayloadCapacityError):
        freeze_payload(
            ModelFieldSnapshot(ExpandingModel(data={"nested": ["x" * 100000]})), 600 * 1024
        )
    deep = "leaf"
    for _ in range(60):
        deep = [deep]
    with pytest.raises(PayloadCapacityError):
        freeze_payload(ModelFieldSnapshot(ArbitraryFields(data=deep)), 32 * 1024 * 1024)
    cyclic = []
    cyclic.append(cyclic)
    with pytest.raises(PayloadCapacityError):
        freeze_payload(ModelFieldSnapshot(ArbitraryFields(data=cyclic)), 32 * 1024 * 1024)
    assert ExpandingModel.serializer_calls == 0
    latency_evidence.update(
        boundary="explicit field snapshot; no SQLite/provider",
        raw_model_rejected=True,
        nested_copy_independent=True,
        over_capacity_rejected=True,
        deep_and_cyclic_rejected=True,
        custom_serializer_calls=0,
    )


async def test_semantic_trace_preserves_original_wire_after_later_observation_change(
    database, monkeypatch, latency_evidence
):
    snapshot = await make_snapshot(database)
    expected_snapshot = snapshot.model_dump(mode="json")
    frozen, _ = freeze_payload(ModelFieldSnapshot(snapshot), 32 * 1024 * 1024)
    assert frozen == expected_snapshot
    observation = _observation(snapshot)
    expected_observation = observation.model_dump(mode="json", exclude={"snapshot"})
    observer = SimpleNamespace(evaluate=AsyncMock(return_value=observation))
    writer = DiagnosticWriter()
    recorder = TraceRecorder(database, writer=writer)
    enter, release = asyncio.Event(), asyncio.Event()
    original_prepare = recorder._prepare_write

    async def barrier(*args):
        enter.set()
        await release.wait()
        return await original_prepare(*args)

    monkeypatch.setattr(recorder, "_prepare_write", barrier)
    await writer.start()
    try:
        assert await TracedSemanticObserver(observer, recorder).evaluate(snapshot) is observation
        await asyncio.wait_for(enter.wait(), 10)
        observation.answers.clear()  # Nested mutable result after producer released it.
        release.set()
        await writer.drain()
        rows = {row.kind: payload for row, payload in await records(database)}
        assert rows["semantic_observation_start"]["snapshot"] == expected_snapshot
        assert rows["semantic_observation_end"]["result"]["observation"] == expected_observation
        assert recorder.record_failures == 0 and writer.failures == 0
        latency_evidence.update(
            boundary="real trace recorder/SQLite; synthetic observer",
            snapshot_wire_equal=True,
            nested_result_copy_independent=True,
            recorder_failures=recorder.record_failures,
            writer_failures=writer.failures,
        )
    finally:
        release.set()
        await writer.close()


async def test_field_snapshot_queued_before_privacy_change_is_not_committed(
    database, monkeypatch, latency_evidence
):
    snapshot = await make_snapshot(database)
    writer = DiagnosticWriter()
    recorder = TraceRecorder(database, writer=writer)
    scope = diagnostic_scope(
        recorder,
        conversation=snapshot.scope.conversation_id,
        source=int(snapshot.focus.ref.event_id.partition(":")[2]),
    )
    enter, release = asyncio.Event(), asyncio.Event()
    prepare = recorder._prepare_write

    async def barrier(*args):
        enter.set()
        await release.wait()
        return await prepare(*args)

    monkeypatch.setattr(recorder, "_prepare_write", barrier)
    await writer.start()
    try:
        await recorder.append(scope, "snapshot_old", {"snapshot": ModelFieldSnapshot(snapshot)})
        await asyncio.wait_for(enter.wait(), 10)
        async with database.immediate_session() as session:
            session.add(ExecutionTraceStateModel(id=1, privacy_generation=1))
        release.set()
        await writer.drain()
        async with database.read_session() as reader:
            rows = list(await reader.scalars(select(ExecutionTraceEntryModel)))
        assert not rows and writer.failures == 1 and scope.coverage.failures == 1
        latency_evidence.update(
            boundary="real trace recorder/writer/SQLite, injected privacy change",
            rows_committed=0,
            writer_failures=writer.failures,
            coverage_failures=scope.coverage.failures,
        )
    finally:
        release.set()
        await writer.close()
