"""Participation facts follow real Main request boundaries, never buy a turn."""

import json
from dataclasses import replace
from unittest.mock import AsyncMock

import httpx
import pytest
from tests.support.agent_backend import StubAgentBackend
from tests.support.work_compaction_capacity_helpers import _runtime, _session
from tests.unit.test_current_activation_material import _saved
from tests.unit.test_history_dispatch_ownership import _scene

from yuki.domain.messages import (
    ChatMessage,
    ChatRequest,
    ChatResponse,
    ChatTool,
    ModelResponseStatus,
    ProviderContinuation,
    ToolCall,
    ToolFunction,
)
from yuki.llm.fake import FakeLLMProvider
from yuki.llm.gemini import GeminiProvider
from yuki.services.context_boundary import ContextBoundary
from yuki.services.turn_execution import TurnExecution, _PrimaryDispatch
from yuki.services.turn_transcript import TurnTranscript
from yuki.web.models import WebMode


class ParticipationBackend(StubAgentBackend):
    def __init__(self, value):
        super().__init__()
        self.value = value
        self.reads = []
        self.response_events = []

    async def participation_context(self, runtime):
        self.reads.append(runtime)
        return self.value

    async def observe_response(self, response, runtime):
        self.response_events.append(runtime.visible_event_ids)

    def parallel_safe(self, name, runtime):
        return True

    def is_side_effecting(self, name, arguments_json, runtime):
        return False

    async def execute_call(self, invocation):
        self.value = {
            "unit_id": "original-discussion",
            "semantic": {"floor": "other", "focus_event_id": 19},
        }
        return '{"ok":true,"data":{"finding":"original result"}}'

    def finalize(self, content, runtime):
        return content


def _participation(message):
    content = message.content or ""
    if "<runtime_context>" not in content:
        return None
    state = json.loads(content.split("<runtime_context>\n", 1)[1].split("\n</runtime_context>")[0])
    return state.get("participation")


@pytest.mark.parametrize("with_work", [False, True])
async def test_tool_round_reads_latest_scoped_facts_after_original_prefix(
    database, tmp_path, with_work
):
    control, _, initial = await _session(database, tmp_path)
    responses = iter(
        (
            ChatResponse("", 0, tool_calls=(ToolCall("read-1", ToolFunction("probe", "{}")),)),
            "quietly finished",
        )
    )
    provider = FakeLLMProvider(lambda request: next(responses))
    runner, runtime = await _runtime(database, control, initial, provider)
    runtime = replace(
        runtime,
        work_control=control if with_work else None,
        fixed_tools=(ChatTool("probe", "Read original evidence", {"type": "object"}),),
        max_model_requests=3,
    )
    backend = ParticipationBackend(
        {"unit_id": "original-discussion", "self": {"engage": "stay", "focus_event_id": 8}}
    )
    result = await runner.run(initial, runtime, backend)

    assert result.model_requests == len(provider.requests) == len(backend.reads) == 2
    first, second = provider.requests
    assert first.messages[-1] == initial[-1]
    first_feedback = next(value for message in first.messages if (value := _participation(message)))
    assert first_feedback["self"]["engage"] == "stay"
    assert second.messages[: len(first.messages)] == first.messages
    assert second.messages[-2].role == "tool"
    assert "original result" in second.messages[-2].content
    assert _participation(second.messages[-1]) == backend.value
    assert second.messages[-1].role == "user"
    assert second.tools == first.tools == runtime.fixed_tools


async def test_new_feedback_does_not_force_another_model_request(database, tmp_path):
    control, _, initial = await _session(database, tmp_path)
    backend = ParticipationBackend({"self": {"engage": "stay"}})

    def finish(request):
        backend.value = {"semantic": {"floor": "other", "focus_event_id": 21}}
        return "done"

    provider = FakeLLMProvider(finish)
    runner, runtime = await _runtime(database, control, initial, provider)
    runtime = replace(runtime, work_control=None, max_model_requests=3)
    result = await runner.run(initial, runtime, backend)
    assert result.text == "done"
    assert result.model_requests == len(provider.requests) == len(backend.reads) == 1


async def test_gemini_signed_tool_round_keeps_wire_prefix_and_original_pairing(database, tmp_path):
    control, _, initial = await _session(database, tmp_path)
    wire = []
    signed_call = {
        "functionCall": {"name": "probe", "args": {}},
        "thoughtSignature": "original-paid-signature",
    }

    def transport(request):
        wire.append(json.loads(request.content))
        part = signed_call if len(wire) == 1 else {"text": "done"}
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {"finishReason": "STOP", "content": {"role": "model", "parts": [part]}}
                ]
            },
        )

    async with httpx.AsyncClient(
        base_url="https://wire.invalid/", transport=httpx.MockTransport(transport)
    ) as client:
        provider = GeminiProvider(
            base_url="https://wire.invalid",
            api_key="synthetic",
            timeout_seconds=1,
            max_retries=0,
            client=client,
        )
        runner, runtime = await _runtime(database, control, initial, provider)
        runtime = replace(
            runtime,
            work_control=None,
            fixed_tools=(ChatTool("probe", "Read original evidence", {"type": "object"}),),
            max_model_requests=3,
        )
        backend = ParticipationBackend({"self": {"engage": "stay"}})
        await runner.run(initial, runtime, backend)

    assert len(wire) == 2
    first, second = wire
    assert second["systemInstruction"] == first["systemInstruction"]
    assert second["tools"] == first["tools"]
    assert second["contents"][: len(first["contents"])] == first["contents"]
    assert second["contents"][len(first["contents"])]["parts"] == [signed_call]
    tail = second["contents"][-1]
    assert tail["role"] == "user"
    assert "original result" in json.dumps(tail, ensure_ascii=False)
    assert any('"floor":"other"' in part.get("text", "") for part in tail["parts"])


async def test_no_scoped_facts_adds_no_runtime_material(database, tmp_path):
    control, _, initial = await _session(database, tmp_path)
    provider = FakeLLMProvider(lambda request: "done")
    runner, runtime = await _runtime(database, control, initial, provider)
    backend = ParticipationBackend(None)
    await runner.run(initial, replace(runtime, work_control=None), backend)
    assert provider.requests[0].messages == initial


async def test_real_main_keeps_participation_feedback_out_of_public_history(database, tmp_path):
    from tests.conftest import MemorySender

    provider = FakeLLMProvider()
    _, harness, chat, _, message = await _scene(database, tmp_path, provider)
    latest = {"thread": "private-current-participation", "self_intent": {"engage": "stay"}}
    chat.participation_runtime_context = AsyncMock(side_effect=lambda runtime: latest)

    def respond(request):
        nonlocal latest
        if len(provider.requests) == 1:
            latest = {
                "thread": "private-current-participation",
                "semantic_review": {"interaction": "other_exchange", "floor": "other"},
            }
            return ChatResponse(
                "",
                0,
                tool_calls=(
                    ToolCall(
                        "original-task",
                        ToolFunction(
                            "task_control", '{"action":"accept","goal":"inspect evidence"}'
                        ),
                    ),
                ),
            )
        return "done"

    provider._responder = respond
    await harness.processor.handle(message, MemorySender())
    assert len(provider.requests) == 2
    first, second = provider.requests
    assert second.messages[: len(first.messages)] == first.messages
    assert _participation(second.messages[-1]) == latest
    assert "private-current-participation" not in (await _saved(database)).payload_json
    assert chat.participation_runtime_context.await_count == 2


async def test_pause_replays_original_checkpoint_then_reads_at_next_normal_boundary(
    database, tmp_path
):
    control, _, initial = await _session(database, tmp_path)
    checkpoint = ProviderContinuation("fake", "responses", ({"id": "original-paid-state"},))
    responses = iter(
        (
            ChatResponse(
                "",
                0,
                status=ModelResponseStatus.INCOMPLETE,
                incomplete_reason="pause_turn",
                continuation=checkpoint,
            ),
            ChatResponse("", 0, tool_calls=(ToolCall("read-1", ToolFunction("probe", "{}")),)),
            "done",
        )
    )
    provider = FakeLLMProvider(lambda request: next(responses))
    runner, runtime = await _runtime(database, control, initial, provider)
    runtime = replace(
        runtime,
        work_control=None,
        fixed_tools=(ChatTool("probe", "Read original evidence", {"type": "object"}),),
        max_model_requests=4,
    )
    backend = ParticipationBackend({"self": {"engage": "stay"}})
    result = await runner.run(initial, runtime, backend)
    assert result.model_requests == len(provider.requests) == 3
    assert len(backend.reads) == 2
    first, paused, normal = provider.requests
    assert paused.messages == first.messages
    assert paused.continuation == checkpoint and paused.continuation_items == ()
    assert _participation(normal.continuation_items[-1]) == backend.value
    assert normal.continuation == checkpoint


async def test_exact_work_recovery_keeps_paid_input_then_allows_later_feedback(database, tmp_path):
    control, session, initial = await _session(database, tmp_path)
    checkpoint = ProviderContinuation("fake", "responses", ({"id": "original-paid-state"},))
    transcript = TurnTranscript(initial)
    transcript.accept(checkpoint)
    transcript.append_result("original-call", "original paired receipt")
    session.transcript = transcript
    session.uses_recovery_transcript = True
    session.exact_recovery = True
    provider = FakeLLMProvider()
    runner, runtime = await _runtime(database, control, initial, provider)
    backend = ParticipationBackend({"semantic": {"floor": "other", "focus_event_id": 23}})
    execution = TurnExecution(runner, initial, runtime, backend)
    execution.state.transcript = transcript
    original = transcript.request()
    replay = await execution.prepare_request(0, (), WebMode.DISABLED)
    assert replay.request.messages == original.messages
    assert replay.request.continuation_items == original.items
    assert replay.request.continuation == checkpoint
    assert not backend.reads
    later = await execution.prepare_request(1, (), WebMode.DISABLED)
    assert later.request.continuation_items[:-1] == original.items
    assert _participation(later.request.continuation_items[-1]) == backend.value
    assert len(backend.reads) == 1 and not provider.requests


@pytest.mark.parametrize("selected", [False, True])
async def test_response_feedback_contains_only_dispatched_boundary_events(
    database, tmp_path, selected
):
    control, _, initial = await _session(database, tmp_path)
    provider = FakeLLMProvider(lambda request: "done")
    runner, runtime = await _runtime(database, control, initial, provider)
    runtime = replace(runtime, work_control=None, visible_event_ids=frozenset({1}))
    backend = ParticipationBackend(None)
    execution = TurnExecution(runner, initial, runtime, backend)
    extra = ChatMessage("user", "prepared original source 2")
    commit = AsyncMock()
    boundary = ContextBoundary((((2,), extra),), commit)
    if selected:
        execution.state.transcript.append(extra)
    sequence = execution.state.transcript.request()
    dispatch = _PrimaryDispatch(
        runner=runner,
        runtime=runtime,
        request_index=0,
        execute=lambda: provider.complete(ChatRequest(messages=sequence.messages)),
        sequence=sequence,
        boundary=boundary,
        observed_event_ids=execution.state.observed_event_ids,
        tools=backend,
    )
    response = await dispatch.complete()
    await execution.observe_response(0, response, ())
    assert backend.response_events == [frozenset({1, 2}) if selected else frozenset({1})]
    assert commit.await_count == int(selected)
    assert runtime.visible_event_ids == frozenset({1})
