"""Only explicit Profile limits bound input; unknown capacity stays unknown."""

import hashlib
import json
from types import SimpleNamespace

import pytest

from yuki.conversation.frozen_fragments import FrozenFragments
from yuki.domain.messages import (
    ChatMessage,
    ChatRequest,
    ProviderContinuation,
    ToolCall,
    ToolFunction,
)
from yuki.llm.base import LLMUnsupportedFeatureError
from yuki.llm.fake import FakeLLMProvider
from yuki.model_runtime.capacity import ModelCapacity, estimate_request_tokens
from yuki.model_runtime.executor import TaskModelExecutor
from yuki.model_runtime.models import ModelProfile, ModelRoute, ModelTask
from yuki.model_runtime.pool import ModelClientPool
from yuki.model_runtime.profiles import ModelProfileCatalog
from yuki.model_runtime.routes import ModelRouter
from yuki.runtime.work_journal import encode_transcript
from yuki.runtime.work_repository import WorkCapacityError
from yuki.services.context_compaction import (
    PublicCompactContext,
    compacted_transcript,
    execution_compact_plan,
    public_compact_plan,
)
from yuki.services.turn_transcript import TurnTranscript


@pytest.mark.parametrize(
    ("capacity", "output_tokens", "expected"),
    [
        (ModelCapacity(), None, None),
        (ModelCapacity(input_tokens=524288), None, 524288),
        (ModelCapacity(context_tokens=524288), None, 516096),
        (ModelCapacity(input_tokens=96000, context_tokens=524288), 16384, 96000),
        (ModelCapacity(input_tokens=524288, context_tokens=128000), 16384, 111616),
        (ModelCapacity(context_tokens=8192), 0, 8192),
        (ModelCapacity(context_tokens=8192), 8192, 0),
        (ModelCapacity(context_tokens=8192), 16384, -8192),
    ],
)
def test_input_budget_uses_only_known_profile_limits(capacity, output_tokens, expected):
    assert capacity.input_budget(output_tokens=output_tokens) == expected


@pytest.mark.parametrize(
    ("limits", "output_tokens", "allowed"),
    [
        ({}, None, True),
        ({"max_input_tokens": 524288}, None, True),
        ({"max_input_tokens": 262144}, None, False),
        ({"context_window_tokens": 8192}, 8192, False),
        ({"context_window_tokens": 8192}, 16384, False),
    ],
)
async def test_executor_checks_real_capacity_before_dispatch(limits, output_tokens, allowed):
    provider = FakeLLMProvider(lambda request: "Original request completed")
    profile = ModelProfile(
        id="main",
        provider="fake",
        model="capacity-test",
        timeout_seconds=1,
        max_retries=0,
        default_temperature=0.7,
        default_max_output_tokens=8192,
        **limits,
    )
    catalog = ModelProfileCatalog(
        profiles={profile.id: profile},
        routes={task: ModelRoute(task=task, profile_id=profile.id) for task in ModelTask},
    )
    executor = TaskModelExecutor(
        router=ModelRouter(catalog),
        pool=ModelClientPool(injected_profiles={profile.id: provider}),
    )
    request = ChatRequest(
        messages=(ChatMessage("user", "x" * 900000),), max_output_tokens=output_tokens
    )
    assert estimate_request_tokens(request) > 262144
    try:
        if allowed:
            response = await executor.execute(ModelTask.CHAT_AGENT, request)
            assert response.content == "Original request completed"
            assert provider.requests[0].messages == request.messages
        else:
            with pytest.raises(LLMUnsupportedFeatureError, match="input capacity"):
                await executor.execute(ModelTask.CHAT_AGENT, request)
            assert provider.requests == []
    finally:
        await executor.close()


def test_unknown_public_capacity_covers_all_events_without_private_material():
    static = ChatMessage("system", "Original Main contract")
    fragments = FrozenFragments.load([]).extend_history(
        tuple(
            ((identity,), ChatMessage("user", "public fact " + "x" * 450000))
            for identity in (41, 42)
        ),
        (),
    )

    async def adopt(*args):
        raise AssertionError("Planning must not publish")

    context = PublicCompactContext(
        (static, *fragments.messages()),
        1,
        fragments,
        SimpleNamespace(
            conversation_id="public-capacity",
            generation=1,
            prompt_source_revision=0,
            starts_after_event_id=0,
        ),
        "public-capacity",
        None,
        adopt,
    )
    initial = (*context.messages, ChatMessage("user", "Private current trigger"))
    transcript = TurnTranscript(initial)
    private = ChatMessage("assistant", "Private execution finding")
    transcript.append(private)
    request = ChatRequest(messages=transcript.request().messages)
    assert estimate_request_tokens(request) > 262144
    plan = public_compact_plan(
        context,
        transcript,
        request,
        initial=initial,
        input_budget=None,
        prepare=lambda request: request,
    )
    assert plan.public["coverage"] == 42
    assert plan.public["items"] == []
    assert plan.request.messages[:-1] == context.messages
    assert plan.retained == (private,)
    assert transcript.request().messages == request.messages


@pytest.mark.parametrize("native", [False, True])
def test_unknown_execution_capacity_keeps_complete_tool_pair_and_original_checkpoint(native):
    initial = (ChatMessage("system", "Original Main contract"), ChatMessage("user", "Task"))
    transcript = TurnTranscript(initial)
    arguments = {"path": "/workspace/original.txt", "offset": 17}
    call = ToolCall("paid-read", ToolFunction("read_probe", json.dumps(arguments)))
    response = ChatMessage("assistant", "", tool_calls=(call,))
    if native:
        transcript.accept(
            ProviderContinuation("gemini", "gemini", ({"opaque": "original-signature"},), "main")
        )
    else:
        transcript.append(response)
    original_output = "original tool result " + "x" * 900000
    original_output_hash = hashlib.sha256(original_output.encode()).hexdigest()
    transcript.append_result(call.id, original_output)

    def transcript_hash(current):
        return hashlib.sha256(
            json.dumps(encode_transcript(current), sort_keys=True).encode()
        ).hexdigest()

    original_checkpoint_hash = transcript_hash(transcript)
    sequence = transcript.request()
    request = ChatRequest(
        messages=sequence.messages,
        continuation=sequence.continuation,
        continuation_items=sequence.items,
    )
    assert estimate_request_tokens(request) > 262144
    plan = execution_compact_plan(
        transcript,
        initial,
        request,
        input_budget=None,
        prepare=lambda request: request,
        responses=(response,) if native else (),
    )
    original_entries = transcript.entries()
    maintenance_entries = plan.transcript.entries()[:-1]
    assert len(maintenance_entries) == len(original_entries)
    prefix_unchanged = all(
        left == right for left, right in zip(maintenance_entries, original_entries, strict=True)
    )
    assert prefix_unchanged, "Maintenance must preserve the original complete protocol prefix"
    assert plan.request.continuation == request.continuation
    assert transcript_hash(transcript) == original_checkpoint_hash

    rebuilt = compacted_transcript(
        {
            "prefix": encode_transcript(TurnTranscript(initial)),
            "retained": encode_transcript(TurnTranscript(plan.retained)),
            "response": "Earlier work summarized; observe the original result next.",
            "covered_entries": len(plan.transcript.entries()) - 1,
        }
    ).request()
    assert rebuilt.continuation is None
    assert rebuilt.messages[: len(initial)] == initial
    if native:
        portable = [
            json.loads(message.content)
            for message in rebuilt.messages
            if (message.content or "").startswith("{")
        ]
        calls = [
            item
            for payload in portable
            if payload.get("kind") == "work_unobserved_tool_round"
            for item in payload["calls"]
            if item["call_id"] == call.id
        ]
        call_count = len(calls)
        assert call_count == 1
        assert calls[0]["tool"] == call.function.name
        assert json.loads(calls[0]["arguments"]) == arguments
        actual_output = calls[0]["result"]
        signature_transplanted = any(
            "original-signature" in (message.content or "") for message in rebuilt.messages
        )
        assert not signature_transplanted
    else:
        raw_calls = [
            item
            for message in rebuilt.messages
            for item in message.tool_calls
            if item.id == call.id
        ]
        raw_call_count = len(raw_calls)
        assert raw_call_count == 1
        assert raw_calls[0].function.name == call.function.name
        assert json.loads(raw_calls[0].function.arguments) == arguments
        results = [message for message in rebuilt.messages if message.tool_call_id == call.id]
        result_count = len(results)
        assert result_count == 1
        actual_output = results[0].content
    actual_length, original_length = len(actual_output), len(original_output)
    assert actual_length == original_length
    actual_output_hash = hashlib.sha256(actual_output.encode()).hexdigest()
    assert actual_output_hash == original_output_hash


@pytest.mark.parametrize("budget", [None, 524288])
def test_unknown_capacity_does_not_make_an_unpaired_call_a_legal_compact_segment(budget):
    initial = (ChatMessage("system", "Original Main contract"),)
    transcript = TurnTranscript(initial)
    transcript.append(
        ChatMessage("assistant", "", tool_calls=(ToolCall("pending", ToolFunction("read", "{}")),))
    )
    with pytest.raises(WorkCapacityError, match="model_request_capacity"):
        execution_compact_plan(
            transcript,
            initial,
            ChatRequest(messages=transcript.request().messages),
            input_budget=budget,
            prepare=lambda request: request,
        )
