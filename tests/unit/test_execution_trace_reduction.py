"""One actual wire body, compact logical metadata, and unchanged business results."""

import json
import time
from contextlib import nullcontext
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
from yuki.domain.messages import ChatMessage, ChatRequest, ToolCall, ToolFunction
from yuki.execution_trace.db_models import ExecutionTraceEntryModel
from yuki.execution_trace.payload import decode_payload
from yuki.execution_trace.phases import collect_phase_metrics
from yuki.execution_trace.recorder import TraceRecorder, trace_span
from yuki.llm.openai_compatible import OpenAICompatibleProvider
from yuki.model_runtime.executor import TaskModelExecutor
from yuki.model_runtime.models import ModelTask
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


@pytest.mark.parametrize("retry", [False, True])
async def test_actual_attempt_body_and_response_remain_while_logical_content_is_removed(
    database, retry
):
    wire, traced_wire = [], []
    answer = {
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
    request = ChatRequest(
        (ChatMessage("system", "stable prefix " * 200), ChatMessage("user", "original input")),
        request_chain_id="original-chain",
        provider_session_id="original-provider-session",
    )

    async def run(traces, requests):
        def transport(incoming):
            requests.append((incoming.content, incoming.headers["x-session-id"]))
            return (
                httpx.Response(503, json={"error": {"type": "temporarily_unavailable"}})
                if retry and len(requests) == 1
                else httpx.Response(200, json=answer)
            )

        async with httpx.AsyncClient(
            base_url="https://offline.invalid", transport=httpx.MockTransport(transport)
        ) as client:
            provider = OpenAICompatibleProvider(
                base_url="https://offline.invalid",
                api_key="synthetic",
                timeout_seconds=1,
                max_retries=int(retry),
                client=client,
            )
            models = TaskModelExecutor(
                router=ModelRouter(catalog()),
                pool=ModelClientPool(injected_profiles={"main": provider}),
                traces=traces,
            )
            try:
                return await models.execute(ModelTask.CHAT_AGENT, request)
            finally:
                await models.close()

    baseline = await run(None, wire)
    actual = await run(TraceRecorder(database), traced_wire)
    assert wire == traced_wire
    assert actual.content == baseline.content == "actual answer"
    assert actual.reasoning_content == baseline.reasoning_content == "actual returned thought"
    expected_cache = None if retry else 80
    assert actual.cached_prompt_tokens == baseline.cached_prompt_tokens == expected_cache
    recorded = await records(database)
    starts = [(row, payload) for row, payload in recorded if row.kind == "provider_start"]
    responses = [(row, payload) for row, payload in recorded if row.kind == "provider_response"]
    assert len(starts) == len(responses) == len(traced_wire)
    for (start, payload), (response, returned), (body, _session) in zip(
        starts, responses, traced_wire, strict=True
    ):
        assert payload["body"] == json.loads(body)
        assert start.operation_id == response.operation_id
        assert returned["http_status"] in {200, 503}
    assert responses[-1][1]["body"] == answer
    assert not any(row.kind == "provider_end" for row, _payload in recorded)
    assert any(row.kind == "provider_error" for row, _payload in recorded) is retry
    logical = {row.kind: payload for row, payload in recorded if row.kind.startswith("model_")}
    assert set(logical) == {"model_phases"}
    assert logical["model_phases"]["request_chain_id"] == "original-chain"
    assert logical["model_phases"]["cached_prompt_tokens"] == expected_cache
    assert logical["model_phases"]["model"] == "offline"
    assert logical["model_phases"]["physical_attempt_count"] == len(traced_wire)
    assert "stable prefix" not in json.dumps(logical)
    assert "actual answer" not in json.dumps(logical)
    assert "actual returned thought" not in json.dumps(logical)


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
    )
    actual = await runner.run(messages, runtime, None)
    assert actual is result
    execute.assert_awaited_once_with(messages, runtime, None)
    payloads = {row.kind: payload for row, payload in await records(database)}
    assert payloads["turn_start"] == {"message_count": 1}
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
            remaining_calls=None,
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
        platform_message_id="semantic-original",
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
