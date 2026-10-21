"""Only explicit Profile limits bound input; unknown capacity stays unknown."""

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
from yuki.runtime.work_repository import WorkCapacityError
from yuki.services.context_compaction import (
    PublicCompactContext,
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
    call = ToolCall("paid-read", ToolFunction("read_probe", "{}"))
    response = ChatMessage("assistant", "", tool_calls=(call,))
    if native:
        transcript.accept(
            ProviderContinuation("gemini", "gemini", ({"opaque": "original-signature"},), "main")
        )
    else:
        transcript.append(response)
    transcript.append_result(call.id, "original tool result " + "x" * 900000)
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
    assert plan.transcript.entries()[:-1] == transcript.entries()
    assert plan.retained == ()
    assert plan.request.continuation == request.continuation
    assert transcript.request() == sequence


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
