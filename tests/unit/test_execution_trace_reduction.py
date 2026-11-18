"""One actual wire body, compact logical metadata, and unchanged business results."""

import hashlib
import json
import time
from contextlib import nullcontext
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import select
from tests.support.event_seed import seed_event
from tests.support.semantic_participation_host_helpers import _observation
from tests.unit.test_provider_dispatch_preparation import catalog
from yuki_participation.models import Scope, ScopedEvent, Snapshot, SourceRef

from yuki.domain.conversations import ScopeType
from yuki.domain.messages import ChatMessage, ChatRequest, ChatTool, ToolCall, ToolFunction
from yuki.execution_trace.db_models import ExecutionTraceEntryModel
from yuki.execution_trace.payload import decode_payload
from yuki.execution_trace.phases import collect_phase_metrics
from yuki.execution_trace.recorder import TraceRecorder, trace_span
from yuki.llm.openai_compatible import OpenAICompatibleProvider
from yuki.model_runtime.executor import TaskModelExecutor
from yuki.model_runtime.models import ModelCapability, ModelTask
from yuki.model_runtime.pool import ModelClientPool
from yuki.model_runtime.routes import ModelRouter
from yuki.persistence.repositories import EventLedgerRepository
from yuki.services.agent_runner import AgentRunner, AgentRunResult
from yuki.services.concurrency import ConcurrencyManager
from yuki.services.participation_trace import TracedSemanticObserver


async def records(database):
    async with database.read_session() as reader:
        rows = list(
            await reader.scalars(
                select(ExecutionTraceEntryModel).order_by(ExecutionTraceEntryModel.id)
            )
        )
    return [
        (
            row,
            decode_payload(row.payload_gzip, size=row.payload_bytes, digest=row.payload_sha256)[
                "data"
            ],
        )
        for row in rows
    ]


async def test_empty_success_has_no_end_row_but_root_exit_keeps_metrics(database):
    traces = TraceRecorder(database)
    async with trace_span("chat_processing", {}, recorder=traces):
        async with trace_span("provider", {"dispatch": "prepared"}):
            collect_phase_metrics(social_transport_seconds=0.25)
    recorded = await records(database)
    assert [row.kind for row, _payload in recorded] == [
        "chat_processing_start",
        "provider_start",
        "phase_metrics",
    ]
    assert recorded[-1][1]["social_transport_seconds"] == 0.25
    assert traces._live_spans == {}


ANSWER = {
    "choices": [
        {
            "message": {
                "role": "assistant",
                "content": "actual answer",
                "reasoning_content": "actual returned thought",
            },
            "finish_reason": "stop",
        }
    ],
    "usage": {
        "prompt_tokens": 100,
        "completion_tokens": 5,
        "total_tokens": 105,
        "prompt_tokens_details": {"cached_tokens": 80},
    },
}
TOOL = ChatTool(
    name="lookup",
    description="Look up the original item.",
    parameters={"type": "object", "properties": {"query": {"type": "string"}}},
)


def _request():
    return ChatRequest(
        (ChatMessage("system", "stable prefix " * 200), ChatMessage("user", "original input")),
        tools=(TOOL,),
        request_chain_id="original-chain",
        provider_session_id="original-provider-session",
    )


async def _wire(traces, outcomes, *, retries=0):
    """Run the actual provider over an offline transport; return the HTTP bodies sent."""
    wire = []

    def transport(incoming):
        wire.append(incoming.content)
        outcome = outcomes[min(len(wire), len(outcomes)) - 1]
        if outcome == "transport":
            raise httpx.ConnectError("synthetic transport loss", request=incoming)
        if outcome == "invalid_json":
            return httpx.Response(200, content=b"not json")
        if outcome == "malformed":
            return httpx.Response(200, json={"choices": []})
        if isinstance(outcome, int):
            return httpx.Response(outcome, json={"error": {"type": "synthetic_rejection"}})
        return httpx.Response(200, json=ANSWER)

    async with httpx.AsyncClient(
        base_url="https://offline.invalid", transport=httpx.MockTransport(transport)
    ) as client:
        provider = OpenAICompatibleProvider(
            base_url="https://offline.invalid",
            api_key="synthetic",
            timeout_seconds=1,
            max_retries=retries,
            client=client,
        )
        models = TaskModelExecutor(
            router=ModelRouter(catalog(capabilities=frozenset({ModelCapability.TOOLS}))),
            pool=ModelClientPool(injected_profiles={"main": provider}),
            traces=traces,
        )
        try:
            return wire, await models.execute(ModelTask.CHAT_AGENT, _request())
        except Exception as exc:
            return wire, exc
        finally:
            await models.close()


def _tools_digest(body):
    tools = json.dumps(body["tools"], ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(tools.encode()).hexdigest()


async def test_successful_attempt_keeps_metadata_usage_and_phases_without_bodies(database):
    baseline_wire, baseline = await _wire(None, ["ok"])
    traced_wire, actual = await _wire(TraceRecorder(database), ["ok"])
    # Tracing never changes the bytes sent to the provider (prefix cache).
    assert traced_wire == baseline_wire
    assert actual.content == baseline.content == "actual answer"
    assert actual.cached_prompt_tokens == baseline.cached_prompt_tokens == 80
    recorded = await records(database)
    kinds = [row.kind for row, _payload in recorded]
    assert "provider_request" not in kinds
    payloads = {row.kind: payload for row, payload in recorded}
    body = json.loads(traced_wire[0])
    start = payloads["provider_start"]
    assert "body" not in start
    assert start["dispatch"] == "prepared"
    assert start["model"] == "offline"
    assert start["request_bytes"] == len(traced_wire[0])
    assert start["tools_sha256"] == _tools_digest(body)
    assert start["tool_count"] == 1
    response = payloads["provider_response"]
    assert "body" not in response
    assert response["http_status"] == 200
    assert response["response_bytes"] > 0
    assert response["usage"]["total_tokens"] == 105
    assert response["usage"]["cached_prompt_tokens"] == 80
    assert payloads["model_phases"]["physical_attempt_count"] == 1
    assert payloads["model_phases"]["cached_prompt_tokens"] == 80
    serialized = json.dumps([payload for _row, payload in recorded])
    assert "stable prefix" not in serialized
    assert "actual answer" not in serialized
    assert "actual returned thought" not in serialized


@pytest.mark.parametrize(
    "outcomes,retries,full_attempts",
    [
        ([400], 0, [0]),  # upstream rejection
        ([503, "ok"], 1, [0, 1]),  # retried failure, then the successful retry
        (["invalid_json"], 0, [0]),
        (["malformed"], 0, [0]),  # HTTP succeeded, the reply could not be used
        (["transport"], 0, [0]),  # no response reached us
    ],
)
async def test_failed_rejected_and_retried_attempts_keep_full_request_and_response(
    database, outcomes, retries, full_attempts
):
    baseline_wire, _ = await _wire(None, outcomes, retries=retries)
    traced_wire, _ = await _wire(TraceRecorder(database), outcomes, retries=retries)
    assert traced_wire == baseline_wire
    recorded = await records(database)
    starts = [row for row, _payload in recorded if row.kind == "provider_start"]
    assert len(starts) == len(traced_wire)
    full = [(row, payload) for row, payload in recorded if row.kind == "provider_request"]
    assert len(full) == len(full_attempts)
    for (_row, payload), index in zip(full, full_attempts, strict=True):
        body = json.loads(traced_wire[index])
        tools = _tools_digest(body)
        del body["tools"]
        # The complete actual body, minus the fixed tool list linked by its digest.
        assert payload["body"] == body
        assert payload["tools_sha256"] == tools
    returned = [payload for row, payload in recorded if row.kind == "provider_response"]
    if outcomes[-1] == "transport":
        assert returned == []
    else:
        assert any("body" in payload for payload in returned)
    if outcomes == [503, "ok"]:
        assert [payload["http_status"] for payload in returned] == [503, 200]
        assert returned[-1]["body"] == ANSWER
        assert returned[0]["body"] == {"error": {"type": "synthetic_rejection"}}


async def test_turn_metadata_does_not_copy_initial_history_or_final_answer(database, monkeypatch):
    traces = TraceRecorder(database)
    models = SimpleNamespace(traces=traces, pin=nullcontext)
    runner = AgentRunner(models, ConcurrencyManager())
    messages = (ChatMessage("user", "full initial history"),)
    result = AgentRunResult("final normalized text", 2, 3, messages_sent=1)
    execute = AsyncMock(return_value=result)
    monkeypatch.setattr(runner, "_run_with_receipts", execute)
    runtime = SimpleNamespace(
        canonical_conversation_id=None,
        execution_id="event:1",
        source_event_id=None,
        origin=SimpleNamespace(value="user_message"),
        manifest_revision="original-manifest",
    )
    actual = await runner.run(messages, runtime, None)
    assert actual is result
    execute.assert_awaited_once_with(messages, runtime, None)
    payloads = {row.kind: payload for row, payload in await records(database)}
    # Provider attempts link their fixed tool list through this frozen manifest.
    assert payloads["turn_start"] == {"message_count": 1, "manifest_revision": "original-manifest"}
    assert payloads["turn_end"]["result"]["messages_sent"] == 1
    assert "full initial history" not in json.dumps(payloads)
    assert "final normalized text" not in json.dumps(payloads)


async def test_tool_details_are_recorded_once_per_item_without_batch_copies(database):
    calls = (ToolCall("original-call", ToolFunction("lookup", '{"query":"original"}')),)
    executed = []

    class Backend:
        async def execute_call(self, invocation):
            executed.append(invocation)
            return '{"ok":true,"result":"original result"}'

        def parallel_safe(self, _name, _runtime):
            return True

        def is_side_effecting(self, _name, _arguments, _runtime):
            return False

        def counts_toward_limit(self, _name, _runtime):
            return True

    runner = AgentRunner(SimpleNamespace(), ConcurrencyManager())
    runtime = SimpleNamespace(
        work_control=None, execution_id="original-execution", script_api=None, manifest_revision=""
    )
    backend = Backend()
    async with trace_span("turn", {}, recorder=TraceRecorder(database)):
        result = await runner._execute_tool_batch(
            calls,
            backend,
            runtime,
            max_parallel_calls=1,
            declared_names=frozenset({"lookup"}),
            chain_id="original-chain",
        )
    assert len(executed) == 1 and executed[0].call == calls[0]
    assert result.calls == ((calls[0], '{"ok":true,"result":"original result"}', True),)
    recorded = await records(database)
    tool = {row.kind: payload for row, payload in recorded if row.kind.startswith("tool")}
    assert set(tool) == {"tool_start", "tool_end"}
    assert tool["tool_start"]["call"]["function"]["arguments"] == calls[0].function.arguments
    assert tool["tool_end"]["result"] == result.calls[0][1]


@pytest.mark.parametrize("failure", [None, "http", "invalid"])
async def test_semantic_snapshot_is_not_duplicated_and_failure_details_remain(database, failure):
    event, _ = await seed_event(
        EventLedgerRepository(database),
        bot_user_id="8000",
        origin_key="semantic-original",
        scope_type=ScopeType.GROUP,
        group_id="2001",
        sender_user_id="1001",
        direction="inbound",
        content="source text",
    )
    now = time.time()
    scope = Scope(conversation_id=event.canonical_conversation_id, generation=1)
    snapshot = Snapshot(
        scope=scope,
        focus=ScopedEvent(
            scope=scope,
            ref=SourceRef(event_id=f"event:{event.id}", revision=1),
            thread="original-thread",
            author=event.author_person_id,
            target=event.author_person_id,
            text="original snapshot",
            at=now,
        ),
        context=(),
        sequence=1,
        issued_at=now,
    )
    observation = _observation(snapshot)
    problem = (
        httpx.HTTPStatusError(
            "synthetic",
            request=httpx.Request("POST", "https://offline.invalid"),
            response=httpx.Response(429),
        )
        if failure == "http"
        else ValueError("synthetic invalid observation")
        if failure == "invalid"
        else None
    )
    observer = SimpleNamespace(evaluate=AsyncMock(return_value=observation, side_effect=problem))
    traced = TracedSemanticObserver(observer, TraceRecorder(database))
    if problem is None:
        assert await traced.evaluate(snapshot) is observation
    else:
        with pytest.raises(type(problem)) as caught:
            await traced.evaluate(snapshot)
        assert caught.value is problem
    observer.evaluate.assert_awaited_once()
    recorded = await records(database)
    payloads = {row.kind: payload for row, payload in recorded}
    assert payloads["semantic_observation_start"]["snapshot"] == snapshot.model_dump(mode="json")
    assert "semantic_result" not in payloads and "semantic_failure" not in payloads
    if problem is None:
        result = payloads["semantic_observation_end"]["result"]
        assert result["observation"] == observation.model_dump(mode="json", exclude={"snapshot"})
    else:
        result = payloads["semantic_observation_error"]["result"]
        assert result["error_category"] == type(problem).__name__
        assert result["http_status"] == (429 if failure == "http" else None)
    assert "original snapshot" not in json.dumps(result)


async def test_trace_diagnostics_expire_after_fourteen_days_by_default(database):
    from yuki.config import Settings

    assert Settings.model_fields["execution_trace_retention_days"].default == 14
    async with trace_span("turn", {}, recorder=TraceRecorder(database)):
        pass
    async with database.read_session() as reader:
        row = await reader.scalar(select(ExecutionTraceEntryModel))
    assert row.expires_at - row.created_at == timedelta(days=14)
