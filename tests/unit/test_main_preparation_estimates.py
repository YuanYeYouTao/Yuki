"""Soft/hard decisions share a preparation-local complete request estimate."""

from copy import deepcopy
from dataclasses import replace

import pytest
from tests.conftest import MemorySender
from tests.unit.test_history_dispatch_ownership import _scene, _tool

from qq_ai_bot.domain.messages import (
    ChatImage,
    ChatMessage,
    ChatRequest,
    ChatTool,
    NativeToolDefinition,
    NativeToolType,
    ProviderContinuation,
)
from qq_ai_bot.llm.fake import FakeLLMProvider
from qq_ai_bot.model_runtime.capacity import ModelCapacity, estimate_request_tokens
from qq_ai_bot.services import main_agent_turns
from qq_ai_bot.services.context_assembler import ContextAssembler


@pytest.mark.parametrize("opaque_native", [False, True])
async def test_complete_candidate_once_boundaries_and_changed_dependencies(
    database, tmp_path, monkeypatch, opaque_native
):
    provider = FakeLLMProvider()
    provider._responder = lambda _: (
        _tool("send_message", {"text": "done"}, "send") if len(provider.requests) == 1 else "done"
    )
    env, harness, chat, _, message = await _scene(database, tmp_path, provider)
    chat._tools.social_service = env.service
    capacity = ModelCapacity(input_tokens=100_000)
    monkeypatch.setattr(chat.runtime.runner._models, "capacity", lambda _: capacity)
    requests, actual_requests, budgets = [], [], {}
    bounded_calls = []
    bounded = ContextAssembler._bounded_history

    def real_bounded(recent, **kwargs):
        view = kwargs.get("prepared_view")
        assert view is not None and view.recent is recent
        bounded_calls.append(view)
        result = bounded(recent, **kwargs)
        assert result == bounded(
            recent, **{k: v for k, v in kwargs.items() if k != "prepared_view"}
        )
        return result

    monkeypatch.setattr(ContextAssembler, "_bounded_history", staticmethod(real_bounded))
    original_compose = chat.runtime.main_turns.compose
    capacity_request = chat.runtime.runner._capacity_request

    async def compose(**kwargs):
        runtime = kwargs["runtime"]
        budgets["hard"] = capacity.input_budget(
            runtime.context.window_tokens, output_tokens=runtime.llm.max_output_tokens
        )
        budgets["planning"] = max(
            1,
            int(
                min(budgets["hard"], runtime.context.compaction_window_tokens)
                * runtime.context.compaction_trigger_ratio
            )
            - 4096,
        )
        with monkeypatch.context() as patch:
            if opaque_native:

                def opaque(request):
                    return replace(
                        capacity_request(request),
                        native_tools=(NativeToolDefinition(NativeToolType.WEB_SEARCH),),
                        continuation=ProviderContinuation(
                            "test", "responses", ({"opaque": "signature"},), "profile-A"
                        ),
                    )

                patch.setattr(chat.runtime.runner, "_capacity_request", opaque)
            return await original_compose(**kwargs)

    monkeypatch.setattr(chat.runtime.main_turns, "compose", compose)

    def estimate(request):
        requests.append(deepcopy(request))
        actual_requests.append(request)
        for part in request.messages:
            text = part.content or ""
            if text.startswith("BOUNDARY:"):
                _, level, delta = text.split(":")
                return budgets[level] + int(delta)
        return estimate_request_tokens(request)

    monkeypatch.setattr(main_agent_turns, "estimate_request_tokens", estimate)
    original_prepare = main_agent_turns.prepare_history
    checked = []

    async def prepare(repository, context, **kwargs):
        # The first two estimates are initial/fixed requests (one if identical).
        # Use the actually generated fixed-prefix request, not a schema+message sum.
        fixed = requests[1] if len(requests) > 1 else requests[0]
        budgets["soft"] = max(budgets["planning"], estimate_request_tokens(fixed))
        soft, hard = kwargs["context_fits"], kwargs["context_hard_fits"]

        def check(candidate, expected=None):
            before = len(requests)
            first = (soft(candidate), hard(candidate))
            assert (soft(candidate), hard(candidate)) == first
            assert len(requests) - before <= 1
            if expected is not None:
                assert first == expected
            return first

        check(context)
        for level in ("soft", "hard"):
            for delta in (-1, 0, 1):
                value = budgets[level] + delta
                candidate = replace(
                    context, history_messages=(ChatMessage("user", f"BOUNDARY:{level}:{delta}"),)
                )
                check(candidate, (value <= budgets["soft"], value <= budgets["hard"]))
        # Same number of messages is not identity. Media, rollup coverage/mode,
        # and nested schema mutations must invalidate the candidate estimate.
        for text in ("same-length-A", "same-length-B"):
            check(replace(context, history_messages=(ChatMessage("user", text),)))
        media = replace(
            context,
            history_messages=(
                ChatMessage("user", "media", images=(ChatImage("data:image/png;base64,AA=="),)),
            ),
        )
        check(media)
        summary = replace(context, rollup_text="same summary", prompt_effective_coverage=5)
        check(summary)
        check(replace(summary, prompt_effective_coverage=6))
        actual = actual_requests[-1]
        if actual.tools:
            original_schema = deepcopy(actual.tools[0].parameters)
            actual.tools[0].parameters["audit_extra"] = "changed nested schema"
            before = len(requests)
            check(replace(summary, prompt_effective_coverage=6))
            assert len(requests) == before + 1
            actual.tools[0].parameters["enum"] = [1] * 30
            check(replace(summary, prompt_effective_coverage=6))
            integer_tokens = estimate_request_tokens(requests[-1])
            before = len(requests)
            actual.tools[0].parameters["enum"] = [True] * 30
            check(replace(summary, prompt_effective_coverage=6))
            assert len(requests) == before + 1
            assert estimate_request_tokens(requests[-1]) > integer_tokens
            actual.tools[0].parameters["enum"] = [0.0] * 30
            check(replace(summary, prompt_effective_coverage=6))
            zero_tokens = estimate_request_tokens(requests[-1])
            before = len(requests)
            actual.tools[0].parameters["enum"] = [-0.0] * 30
            check(replace(summary, prompt_effective_coverage=6))
            assert len(requests) == before + 1
            assert estimate_request_tokens(requests[-1]) > zero_tokens
            actual.tools[0].parameters.clear()
            actual.tools[0].parameters.update(original_schema)
        if opaque_native:
            assert all(
                r.continuation.profile_id == "profile-A" and r.native_tools for r in requests
            )
            payload = actual.continuation.payload[0]
            payload["numbers"] = [1] * 30
            check(replace(summary, prompt_effective_coverage=6))
            before = len(requests)
            payload["numbers"] = [True] * 30
            check(replace(summary, prompt_effective_coverage=6))
            assert len(requests) == before + 1
            del payload["numbers"]
        # Return to the original prepared request; its frozen initial estimate
        # remains reusable after unrelated candidate checks.
        before = len(requests)
        check(context)
        assert len(requests) == before
        checked.append(True)
        return await original_prepare(repository, context, **kwargs)

    monkeypatch.setattr(main_agent_turns, "prepare_history", prepare)
    assert (await harness.processor.handle(message, MemorySender())).reason == "chat"
    assert checked == [True]
    assert len(provider.requests) == 2
    assert len(bounded_calls) == 1


@pytest.mark.parametrize("other", [True, 1.0])
def test_equal_python_numbers_are_not_equal_serialized_request_input(other):
    first = ChatRequest(messages=(), tools=(ChatTool("t", "", {"enum": [1] * 30}),))
    changed = replace(first, tools=(ChatTool("t", "", {"enum": [other] * 30}),))
    assert first == changed
    assert estimate_request_tokens(first) != estimate_request_tokens(changed)
    assert not main_agent_turns._same_request_input(first, changed)


def test_unknown_opaque_equality_is_not_an_estimate_proof():
    class Opaque:
        def __eq__(self, _other):
            return True

    assert not main_agent_turns._same_request_input(Opaque(), Opaque())


def test_float_zero_sign_is_part_of_serialized_estimate():
    first = ChatRequest(messages=(), tools=(ChatTool("t", "", {"default": [0.0] * 30}),))
    changed = replace(first, tools=(ChatTool("t", "", {"default": [-0.0] * 30}),))
    assert first == changed
    assert estimate_request_tokens(first) != estimate_request_tokens(changed)
    assert not main_agent_turns._same_request_input(first, changed)
