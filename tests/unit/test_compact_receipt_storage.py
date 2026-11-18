"""Ordinary compact receipts keep one paid copy and minimal phase facts.

Failure cases written before the storage change:

- dispatched persists a transcript although recovery only needs the unknown-paid fact;
- the dispatched/response/paired rows repeat the same context (prefix, layout,
  request.messages, original, candidate) and inline images several times;
- a process lost after dispatch must still refuse to rebuy for the original execution ID;
- after paired, reading the receipt must still return the actual paid result;
- a known invalid checkpoint must still be reused for the same candidate;
- receipts written by the previous full-state format must keep restoring;
- none of this may change the request that is sent to the model afterwards.
"""

import copy
import json
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select
from tests.support.compact_delivery_evidence import capture_compact_facts
from tests.support.compact_delivery_evidence import (
    compact_evidence as capture_compact_evidence,
)
from tests.support.work_compaction_capacity_helpers import _runtime, _session
from tests.unit.test_current_context_compaction import _append_public, _public_context

from yuki.conversation.observation_models import ContextObservationModel
from yuki.domain.messages import ChatMessage, ChatRequest, ChatResponse, ModelResponseStatus
from yuki.llm.fake import FakeLLMProvider
from yuki.model_runtime.models import ModelExecutionPriority
from yuki.runtime.work_journal import JournalUnavailable, decode_transcript, encode_transcript
from yuki.services.context_compaction import CompactReceipt, compacted_transcript
from yuki.services.turn_execution import TurnExecution
from yuki.web.models import WebMode

PHASE_FACT_KEYS = {"phase", "ordinal", "source_revision", "privacy_generation"}
DERIVABLE_COPIES = {"request", "layout", "prefix", "candidate"}


@pytest.fixture
def compact_evidence(request):
    with capture_compact_evidence(request) as evidence:
        yield evidence


async def _rows(database, execution_id):
    async with database.read_session() as reader:
        rows = list(
            await reader.scalars(
                select(ContextObservationModel)
                .where(ContextObservationModel.source_key.startswith(f"compact:{execution_id}:"))
                .order_by(ContextObservationModel.created_at)
            )
        )
    return {row.source_key.rsplit(":", 1)[1]: json.loads(row.payload_json) for row in rows}


async def _ordinary(database, tmp_path, responder, execution_id):
    control, _, initial = await _session(database, tmp_path)
    context = await _public_context(database, control, initial)
    initial = (*context.messages, ChatMessage("user", "private runtime and trigger"))
    provider = FakeLLMProvider(responder)
    runner, runtime = await _runtime(database, control, initial, provider)
    runtime = replace(
        runtime,
        work_control=None,
        public_compaction=context,
        execution_id=execution_id,
        compact_focus="",
        auxiliary_requests=[0],
    )
    return control, context, initial, provider, runner, runtime


def _capture_writes(monkeypatch):
    """Record the full in-memory state each phase was asked to persist."""
    written = []
    write = CompactReceipt.write

    async def capture(self, payload, **kwargs):
        written.append(copy.deepcopy(payload))
        await write(self, payload, **kwargs)

    monkeypatch.setattr(CompactReceipt, "write", capture)
    return written


async def _insert_legacy(database, context, actor_id, execution_id, states):
    """Rows exactly as the previous format stored them: the whole state per phase."""
    version = context.version
    created = datetime.now(UTC)
    async with database.immediate_session() as writer:
        for offset, state in enumerate(states):
            payload = dict(state)
            if payload["phase"] == "paired":
                candidate = (
                    compacted_transcript(payload)
                    if payload["valid"]
                    else decode_transcript(payload["original"])
                )
                payload["candidate"] = encode_transcript(candidate)
            writer.add(
                ContextObservationModel(
                    id=str(uuid4()),
                    conversation_id=version.conversation_id,
                    generation=version.generation,
                    actor_id=actor_id,
                    read_scope=version.scope.key,
                    source_key=f"compact:{execution_id}:{payload['ordinal']}:{payload['phase']}",
                    version=1,
                    privacy_generation=payload["privacy_generation"],
                    payload_json=json.dumps(payload, ensure_ascii=False),
                    parent_sources_json="[]",
                    created_at=created + timedelta(microseconds=offset),
                )
            )


async def test_paired_compact_keeps_one_paid_copy_and_minimal_phase_facts(
    database, tmp_path, compact_evidence
):
    _control, context, initial, provider, runner, runtime = await _ordinary(
        database, tmp_path, lambda request: "Public decision checkpoint.", "event:storage"
    )
    execution = TurnExecution(runner, initial, runtime, None)
    request = ChatRequest(messages=initial, max_output_tokens=1024)
    await execution.compact_current(0, request, ModelExecutionPriority.FOREGROUND, 10000)
    directory, evidence = compact_evidence
    try:
        rows = await _rows(database, runtime.execution_id)
        assert set(rows) == {"dispatched", "response", "paired"}
        assert set(rows["dispatched"]) == PHASE_FACT_KEYS
        assert set(rows["paired"]) <= PHASE_FACT_KEYS
        response = rows["response"]
        assert not DERIVABLE_COPIES & set(response)
        # A valid result does not need the pre-compaction input or the raw model reply.
        assert "original" not in response and "model_response" not in response
        assert response["valid"] is True
        assert response["response"] == "Public decision checkpoint."
        assert len(response["request_digest"]) == 64
        stored = sum(len(json.dumps(row, ensure_ascii=False)) for row in rows.values())
        evidence["receipt_bytes"] = stored

        saved = await CompactReceipt(context, runtime.execution_id, runtime.actor_user_id).read()
        assert saved["phase"] == "paired"
        assert saved["valid"] is True
        assert saved["response"] == "Public decision checkpoint."
        assert saved["public"] == response["public"]
        assert saved["retained"] == response["retained"]

        recovered = TurnExecution(runner, initial, runtime, None)
        result = await recovered.restore_compact_receipt()
        assert result.text == "已压缩当前会话的公共上下文。"
        assert len(provider.requests) == 1
        evidence["restored_transcript"] = asdict(recovered.state.transcript.request())
    finally:
        evidence["requests"] = [asdict(item) for item in provider.requests]
        await capture_compact_facts(database, directory, evidence)


async def test_lost_process_after_dispatch_keeps_only_unknown_paid_fact(database, tmp_path):
    def lost(request):
        raise RuntimeError("process_lost_after_dispatch")

    _control, _context, initial, provider, runner, runtime = await _ordinary(
        database, tmp_path, lost, "event:lost-dispatch"
    )
    execution = TurnExecution(runner, initial, runtime, None)
    request = ChatRequest(messages=initial, max_output_tokens=1024)
    with pytest.raises(Exception):  # noqa: B017 - any loss after the real dispatch
        await execution.compact_current(0, request, ModelExecutionPriority.FOREGROUND, 10000)
    rows = await _rows(database, runtime.execution_id)
    assert set(rows) == {"dispatched"}
    assert set(rows["dispatched"]) == PHASE_FACT_KEYS
    recovered = TurnExecution(runner, initial, runtime, None)
    with pytest.raises(JournalUnavailable, match="context_compaction_outcome_unknown"):
        await recovered.restore_compact_receipt()
    assert len(provider.requests) == 1


@pytest.mark.parametrize("outcome", ["valid", "incomplete"])
@pytest.mark.parametrize("phase", ["response", "paired"])
async def test_legacy_full_receipts_restore_the_same_transcript(
    database, tmp_path, monkeypatch, outcome, phase
):
    def answer(request):
        if outcome == "valid":
            return "Public decision checkpoint."
        return ChatResponse(
            content="An incomplete paid checkpoint.",
            latency_seconds=0,
            status=ModelResponseStatus.INCOMPLETE,
            incomplete_reason="max_output_tokens",
        )

    _control, context, initial, provider, runner, runtime = await _ordinary(
        database, tmp_path, answer, "event:current-format"
    )
    written = _capture_writes(monkeypatch)
    execution = TurnExecution(runner, initial, runtime, None)
    await execution._finalize_initial_layout()
    request = ChatRequest(messages=execution.state.transcript.request().messages)
    if phase == "response":
        write = CompactReceipt.write

        async def fail_pairing(self, payload, **kwargs):
            if payload["phase"] == "paired":
                raise RuntimeError("pairing_lost")
            await write(self, payload, **kwargs)

        monkeypatch.setattr(CompactReceipt, "write", fail_pairing)
        with pytest.raises(RuntimeError, match="pairing_lost"):
            await execution.compact_current(0, request, ModelExecutionPriority.FOREGROUND, 10000)
    else:
        await execution.compact_current(0, request, ModelExecutionPriority.FOREGROUND, 10000)
    legacy_states = [state for state in written if state["phase"] in {"dispatched", "response"}]
    if phase == "paired":
        legacy_states.append(next(state for state in written if state["phase"] == "paired"))
    await _insert_legacy(
        database, context, runtime.actor_user_id, "event:legacy-format", legacy_states
    )
    monkeypatch.undo()

    async def restore(execution_id):
        recovered = TurnExecution(
            runner, initial, replace(runtime, execution_id=execution_id), None
        )
        await recovered.restore_compact_receipt()
        return asdict(recovered.state.transcript.request()), recovered.initial_messages

    legacy = await restore("event:legacy-format")
    current = await restore("event:current-format")
    assert current == legacy
    assert len(provider.requests) == 1
    rows = await _rows(database, "event:current-format")
    assert set(rows["paired"]) <= PHASE_FACT_KEYS


async def test_known_invalid_checkpoint_is_reused_for_current_and_legacy_receipts(
    database, tmp_path, monkeypatch
):
    control, _, initial = await _session(database, tmp_path)
    await _append_public(database, "An unchanged public decision. " * 12000)
    context = await _public_context(database, control, initial)
    initial = (*context.messages, ChatMessage("user", "Current request"))
    provider = FakeLLMProvider(lambda request: "")
    runner, runtime = await _runtime(database, control, initial, provider)
    runtime = replace(
        runtime,
        work_control=None,
        public_compaction=context,
        execution_id="ordinary-invalid-current",
        auxiliary_requests=[0],
    )
    written = _capture_writes(monkeypatch)
    execution = TurnExecution(runner, initial, runtime, None)
    first = await execution.prepare_request(0, (), WebMode.DISABLED)
    assert len(provider.requests) == 1
    monkeypatch.undo()
    rows = await _rows(database, runtime.execution_id)
    paired = await CompactReceipt(context, runtime.execution_id, runtime.actor_user_id).read()
    assert paired["phase"] == "paired" and paired["valid"] is False
    assert "request" not in rows["response"] and len(rows["response"]["request_digest"]) == 64
    # The empty paid reply and the original input remain the facts of this failure.
    assert paired["model_response"]["content"] == ""
    assert "original" in rows["response"]

    await _insert_legacy(
        database,
        context,
        runtime.actor_user_id,
        "ordinary-invalid-legacy",
        [state for state in written],
    )
    for execution_id in ("ordinary-invalid-current", "ordinary-invalid-legacy"):
        selected = replace(runtime, execution_id=execution_id, auxiliary_requests=[0])
        recovered = TurnExecution(runner, initial, selected, None)
        await recovered.restore_compact_receipt()
        prepared = await recovered.prepare_request(0, (), WebMode.DISABLED)
        assert (*prepared.request.messages[:-2], prepared.request.messages[-1]) == (
            *first.request.messages[:-2],
            first.request.messages[-1],
        )
    assert len(provider.requests) == 1
