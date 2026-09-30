"""Actual Jev adapter and Host admissions share expiring diagnostics, not recovery state."""

import json
import time

import httpx
import pytest
from tests.unit.test_control_plane_foundation import context
from tests.unit.test_execution_trace import decoded, rows
from tests.unit.test_semantic_participation_host import _event_and_route, _host, _item, _proposal
from yuki_participation.models import Scope, ScopedEvent, Snapshot, SourceRef
from yuki_participation.observer import JevObserver

from qq_ai_bot.control_plane import ControlQueryError, ControlQueryService, PageRequest, ProblemCode
from qq_ai_bot.control_plane.query_types import ExecutionTraceFilter
from qq_ai_bot.domain.identity import ConversationId
from qq_ai_bot.execution_trace.recorder import TraceRecorder, record_trace, trace_span
from qq_ai_bot.persistence.control_query import ControlQueryAdapter
from qq_ai_bot.persistence.repositories import EventLedgerRepository
from qq_ai_bot.services.participation_trace import TracedSemanticObserver


async def snapshot(database):
    event = await _event_and_route(database, EventLedgerRepository(database))
    scope = Scope(conversation_id=event.canonical_conversation_id, generation=0)
    now = time.time()
    return Snapshot(
        scope=scope,
        sequence=1,
        context=(),
        issued_at=now,
        focus=ScopedEvent(
            scope=scope,
            ref=SourceRef(event_id=f"event:{event.id}", revision=1),
            thread=f"event:{event.id}",
            author="SELF",
            target="group",
            text="离线观察内容",
            at=now - 1,
        ),
    )


async def test_actual_jev_probabilities_usage_identity_and_origin_bound_pages(database):
    source = await snapshot(database)
    calls = []

    def respond(request):
        body = json.loads(request.content)
        calls.append(body)
        return httpx.Response(
            200,
            json={
                "model": body["model"],
                "answers": {
                    name: {
                        "type": "choice",
                        "choice": "unknown",
                        "probabilities": {
                            option: float(option == "unknown") for option in question["criteria"]
                        },
                    }
                    for name, question in body["questions"].items()
                },
                "usage": {"input_tokens": 21, "output_tokens": 5},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        original = JevObserver("fixture-secret-not-recorded", client=client)
        wrapped = TracedSemanticObserver(original, TraceRecorder(database))
        prepared = wrapped.prepare_snapshot(source)
        observation = await wrapped.evaluate(prepared)
    assert len(calls) == 1 and observation.snapshot == prepared == source
    recorded = await rows(database)
    assert [row.kind for row in recorded] == [
        "semantic_observation_start",
        "semantic_result",
        "semantic_observation_end",
    ]
    assert all(
        row.source_event_id == int(source.focus.ref.event_id.split(":")[1]) for row in recorded
    )
    payload = decoded(recorded[1])["data"]
    assert payload["observation"] == observation.model_dump(mode="json")
    assert payload["elapsed_seconds"] >= 0
    assert "fixture-secret-not-recorded" not in json.dumps([decoded(row) for row in recorded])
    # A different trace origin must not enter the Jev timeline or accept its cursor.
    async with trace_span("main", {}, recorder=TraceRecorder(database), origin="other"):
        await record_trace("tool_result", {"result": "other"})
    queries = ControlQueryService(ControlQueryAdapter(database))
    ctx = context("control.execution.metadata.read")
    scope = ExecutionTraceFilter(
        origin="semantic_observation",
        conversation_id=ConversationId.parse(source.scope.conversation_id),
    )
    first = await queries.list_execution_trace(ctx, PageRequest(limit=2), scope=scope)
    second = await queries.list_execution_trace(
        ctx, PageRequest(limit=2, cursor=first.next_cursor), scope=scope
    )
    assert len(first.items) == 2 and len(second.items) == 1
    assert all(row.payload is None for row in (*first.items, *second.items))
    with pytest.raises(ControlQueryError) as exc:
        await queries.list_execution_trace(
            ctx, PageRequest(cursor=first.next_cursor), scope=ExecutionTraceFilter(origin="other")
        )
    assert exc.value.problem.code is ProblemCode.VALIDATION_ERROR
    with pytest.raises(ControlQueryError) as exc:
        await queries.read_execution_trace(ctx, recorded[1].id)
    assert exc.value.problem.code is ProblemCode.CAPABILITY_DENIED


async def test_provider_error_is_preserved_without_request_secret_or_private_error(database):
    source = await snapshot(database)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(401, json={"private": "private-server-error"})
        )
    ) as client:
        wrapped = TracedSemanticObserver(
            JevObserver("secret", client=client), TraceRecorder(database)
        )
        with pytest.raises(httpx.HTTPStatusError) as exc:
            await wrapped.evaluate(source)
        assert exc.value.response.status_code == 401
    records = await rows(database)
    assert decoded(records[1])["data"]["http_status"] == 401
    assert records[-1].kind == "semantic_observation_error"
    assert "private-server-error" not in json.dumps([decoded(row) for row in records])


async def test_host_admission_records_original_outcome_and_does_not_duplicate_work(
    database, tmp_path
):
    event = await _event_and_route(database, EventLedgerRepository(database))
    host, _ = await _host(database, tmp_path)
    host._traces = TraceRecorder(database)
    try:
        item = await _item(host, event)
        binding = await host._binding(item)
        source = item.controller.state.events[f"event:{event.id}"]
        proposal = _proposal(item, binding, source)
        await host._admit(item, binding, proposal)
        accepted = await host.repository.query_proposal(
            conversation_id=proposal.scope.conversation_id,
            generation=proposal.scope.generation,
            owner=binding.effective_owner,
            controller_epoch=binding.controller_epoch,
            proposal_id=proposal.proposal_id,
        )
        assert accepted is not None
        entries = await rows(database)
        assert [row.kind for row in entries] == [
            "participation_decision_start",
            "participation_decision_end",
        ]
        assert decoded(entries[0])["data"]["proposal"]["proposal_id"] == proposal.proposal_id
        assert decoded(entries[1])["data"]["result"]["run_id"] == accepted.run_id
        await host._admit(item, binding, proposal)
        assert len(await host.repository.list_active()) == 1
        assert (await rows(database))[-1].origin == "participation_decision"
    finally:
        await host._store.close()


async def test_diagnostic_failure_never_discards_successful_jev_response(database, monkeypatch):
    source = await snapshot(database)

    class Observer:
        calls = 0

        async def evaluate(self, value):
            from tests.unit.test_semantic_participation_host import _observation

            self.calls += 1
            return _observation(value)

    observer = Observer()
    recorder = TraceRecorder(database)
    from qq_ai_bot.execution_trace import recorder as module

    monkeypatch.setattr(
        module, "encode_payload", lambda *_: (_ for _ in ()).throw(ValueError("failed"))
    )
    observation = await TracedSemanticObserver(observer, recorder).evaluate(source)
    assert observation.snapshot == source and observer.calls == 1
    assert recorder.record_failures == 3 and not await rows(database)
