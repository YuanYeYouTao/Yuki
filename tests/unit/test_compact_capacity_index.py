"""Exact additive input costs and latest legal compact boundaries."""

from dataclasses import replace

import pytest

from yuki.domain.messages import (
    ChatImage,
    ChatMessage,
    ChatRequest,
    ChatTool,
    ProviderContinuation,
    ToolCall,
    ToolFunction,
)
from yuki.model_runtime.capacity import (
    RequestTokenCounter,
    estimate_request_tokens,
    message_token_cost,
)
from yuki.runtime.work_repository import WorkCapacityError
from yuki.services.context_compaction import EXECUTION_COMPACT_INSTRUCTION, execution_compact_plan
from yuki.services.turn_transcript import TurnTranscript


@pytest.mark.parametrize("protocol", ["gemini", "anthropic_messages", "responses"])
def test_incremental_cost_matches_full_serialization_for_every_protocol_prefix(protocol):
    image = ChatImage("data:image/png;base64,AAAA")
    transcript = TurnTranscript(
        (
            ChatMessage("system", 'fixed "instructions"\n 中文🙂'),
            ChatMessage("user", "pixels", images=(image,)),
        )
    )
    for ordinal, size in enumerate((1500, 10)):
        transcript.accept(
            ProviderContinuation(
                protocol,
                protocol,
                (
                    {
                        "role": "model",
                        "parts": [
                            {"text": "opaque中" * size},
                            {"inlineData": {"data": "AAAA" * size}},
                        ],
                    },
                ),
                "main",
            )
        )
        transcript.append_result(f"read-{ordinal}", '{"data_url":"business JSON, not media"}')
        transcript.append(ChatMessage("user", "next\n边界"))
    settings = ChatRequest(
        (),
        model="profile-model",
        max_output_tokens=512,
        temperature=0.3,
        tools=(
            ChatTool(
                "read",
                "Schema data_url is not media",
                {
                    "type": "object",
                    "properties": {"data_url": {"type": "string"}},
                },
            ),
        ),
        response_format={"type": "json_schema", "schema": {"data_url": "declaration"}},
    )
    counter = RequestTokenCounter(settings)
    instruction = ChatMessage("user", EXECUTION_COMPACT_INSTRUCTION)
    extra = message_token_cost(instruction)
    original = transcript.entries()
    for boundary, entry in enumerate(original, start=1):
        counter.append(entry)
        prefix = transcript.prefix(boundary)
        prefix.append(instruction)
        sequence = prefix.request()
        request = replace(
            settings,
            messages=sequence.messages,
            continuation=sequence.continuation,
            continuation_items=sequence.items,
        )
        assert counter.estimate(extra) == estimate_request_tokens(request)
    assert transcript.entries() == original


def test_latest_fitting_boundary_survives_a_decreasing_opaque_checkpoint():
    initial = (ChatMessage("system", "contract"), ChatMessage("user", "task"))
    transcript = TurnTranscript(initial)
    responses = []
    for ordinal, size in enumerate((30000, 3000)):
        call = ToolCall(f"read-{ordinal}", ToolFunction("read", "{}"))
        response = ChatMessage("assistant", "finding", tool_calls=(call,))
        responses.append(response)
        transcript.accept(
            ProviderContinuation("gemini", "gemini", ({"signature": "中" * size},), "main")
        )
        transcript.append_result(call.id, "original result")
        transcript.append(ChatMessage("user", "continue"))
    boundary = len(transcript.entries())
    # The unseen original round and late input must fit the next Main too.
    # Keep the latest opaque prefix larger than its portable retained evidence.
    late = ChatMessage("user", "late input " + "x" * 1000)
    transcript.append(late)
    sequence = transcript.request()
    request = ChatRequest(
        sequence.messages,
        model="profile-model",
        continuation=sequence.continuation,
        continuation_items=sequence.items,
    )
    fitting = transcript.prefix(boundary)
    fitting.append(ChatMessage("user", EXECUTION_COMPACT_INSTRUCTION))
    selected = fitting.request()
    budget = estimate_request_tokens(
        replace(
            request,
            messages=selected.messages,
            continuation=selected.continuation,
            continuation_items=selected.items,
        )
    )
    plan = execution_compact_plan(
        transcript,
        initial,
        request,
        input_budget=budget,
        prepare=lambda request: request,
        responses=tuple(responses),
    )
    assert plan.transcript.entries() == fitting.entries()
    assert plan.request.continuation == request.continuation
    assert plan.retained[-1] == late
    assert '"call_id": "read-1"' in plan.retained[0].content
    assert "original result" in plan.retained[0].content
    assert estimate_request_tokens(plan.request) == budget


def test_irreducible_tool_result_preserves_original_tail():
    count = 16
    initial = (ChatMessage("system", "contract"),)
    transcript = TurnTranscript(initial)
    for ordinal in range(count):
        transcript.append(ChatMessage("user", f"retained fact {ordinal}"))
    call = ToolCall("original-read", ToolFunction("read", "{}"))
    transcript.append(ChatMessage("assistant", "", tool_calls=(call,)))
    transcript.append_result(call.id, "original paid result " + "x" * 30000)
    with pytest.raises(WorkCapacityError, match="model_request_capacity"):
        execution_compact_plan(
            transcript,
            initial,
            ChatRequest(transcript.request().messages),
            input_budget=1200,
            prepare=lambda request: request,
        )
    assert transcript.entries()[-1].content.endswith("x" * 30000)
