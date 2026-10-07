"""Each deliberate difference from Pi, as an executable fixture through real Yuki code."""

import asyncio
import json

import pytest
from tests.conftest import build_harness, make_settings
from tests.unit.test_agent_core_differential import Backend, runtime_for
from tests.unit.test_capability_runtime_security import _descriptor, _entry

from qq_ai_bot.capabilities.coordinator import ToolInvocationCoordinator
from qq_ai_bot.capabilities.validation import (
    TOOL_INPUT_VALIDATION_FAILED,
    JsonSchemaCapabilityValidator,
)
from qq_ai_bot.domain.messages import ChatMessage, ChatResponse, ChatTool, ToolCall, ToolFunction
from qq_ai_bot.llm.fake import FakeLLMProvider


def call(identity, name="read", arguments="{}"):
    return ToolCall(identity, ToolFunction(name, arguments))


def test_strict_schema_never_coerces_like_typebox():
    # Pi validateToolArguments coerces "1" -> 1 for numeric schemas. Yuki rejects.
    validator = JsonSchemaCapabilityValidator()
    assert validator.admit((_entry(_descriptor("web_search", namespace="web.search")),)) == ()
    rejected = validator.validate("web_search", json.dumps({"query": 1}))
    assert rejected.ok is False and rejected.error_category == TOOL_INPUT_VALIDATION_FAILED
    assert validator.validate("web_search", json.dumps({"query": "1"})).ok


async def test_declarations_are_fixed_not_dynamically_announced(database):
    # Pi declareToolChanges appends a system message whenever context.tools
    # changes. Yuki's fixed manifest is resent unchanged and no delta is injected.
    responses = iter([ChatResponse("", 0, tool_calls=(call("a"),)), ChatResponse("done", 0)])
    provider = FakeLLMProvider(lambda _request: next(responses))
    chat = build_harness(database, make_settings(database.url), provider).processor._chat
    fixed = (ChatTool("read", "read", {"type": "object"}),)

    class Shifting(Backend):
        calls = 0

        def definitions(self, runtime, **kwargs):
            Shifting.calls += 1
            return (ChatTool(f"dynamic_{Shifting.calls}", "x", {"type": "object"}),)

    runtime = runtime_for(chat, await chat._runtime_config.snapshot(), fixed_tools=fixed)
    result = await chat.runtime.runner.run((ChatMessage("user", "go"),), runtime, Shifting())
    assert result.text == "done"
    assert [tuple(t.name for t in r.tools) for r in provider.requests] == [("read",), ("read",)]
    second = provider.requests[1].messages
    assert not any(m.role == "system" and "dynamic" in (m.content or "") for m in second)


class Concurrency(Backend):
    def __init__(self):
        super().__init__()
        self.active = 0
        self.peak = 0
        self.timeline = []

    async def execute_call(self, invocation):
        name = invocation.call.function.name
        arguments = invocation.call.function.arguments
        self.active += 1
        self.peak = max(self.peak, self.active)
        self.timeline.append(("start", name, arguments))
        await asyncio.sleep(0.01)
        self.timeline.append(("end", name, arguments))
        self.active -= 1
        return json.dumps({"ok": True})


async def test_parallel_reads_are_bounded_not_promise_all():
    backend = Concurrency()
    calls = tuple(call(f"r{i}", arguments=json.dumps({"i": i})) for i in range(6))
    result = await ToolInvocationCoordinator().execute_batch(
        calls, backend, None, remaining_calls=10, max_parallel_calls=2
    )
    assert backend.peak == 2
    # Results stay in model call order regardless of completion order.
    assert [c.id for c, _, _ in result.calls] == [c.id for c in calls]


async def test_send_is_a_barrier_between_read_stretches():
    backend = Concurrency()
    calls = (
        call("r1", arguments='{"i":1}'),
        call("r2", arguments='{"i":2}'),
        call("s", "send_message"),
        call("r3", arguments='{"i":3}'),
    )
    await ToolInvocationCoordinator().execute_batch(
        calls, backend, None, remaining_calls=10, max_parallel_calls=8
    )
    order = [(kind, args) for kind, _name, args in backend.timeline]
    send_start = order.index(("start", "{}"))
    assert {("end", '{"i":1}'), ("end", '{"i":2}')} <= set(order[:send_start])
    assert order.index(("end", "{}")) < order.index(("start", '{"i":3}'))


async def test_lifecycle_control_cannot_share_a_batch(database):
    responses = iter(
        [
            ChatResponse(
                "",
                0,
                tool_calls=(
                    call("ctl", "task_control", '{"action":"complete"}'),
                    call("r", arguments='{"i":1}'),
                ),
            ),
            ChatResponse("done", 0),
        ]
    )
    provider = FakeLLMProvider(lambda _request: next(responses))
    chat = build_harness(database, make_settings(database.url), provider).processor._chat

    class WithControl(Backend):
        def definitions(self, runtime, **kwargs):
            return (
                *super().definitions(runtime),
                ChatTool("task_control", "c", {"type": "object"}),
            )

    backend = WithControl()
    runtime = runtime_for(chat, await chat._runtime_config.snapshot())
    await chat.runtime.runner.run((ChatMessage("user", "go"),), runtime, backend)
    assert backend.executed == []
    receipts = [m.content for m in provider.requests[1].messages if m.role == "tool"]
    assert [json.loads(r)["error"] for r in receipts] == ["work_control_requires_single_call"] * 2


@pytest.mark.parametrize(
    "receipt",
    [
        {"ok": False, "error": "target_not_found", "executed": True, "mutation_committed": False},
        {"ok": False, "status": "unknown", "uncertain": True, "replay_forbidden": True},
    ],
)
async def test_business_failure_receipt_reaches_model_unflattened(database, receipt):
    # Pi turns a thrown tool error into createErrorToolResult(message). Yuki keeps
    # the original typed receipt, including unknown/uncertain, byte for byte.
    responses = iter(
        [ChatResponse("", 0, tool_calls=(call("w", "send_message"),)), ChatResponse("ok", 0)]
    )
    provider = FakeLLMProvider(lambda _request: next(responses))
    chat = build_harness(database, make_settings(database.url), provider).processor._chat
    body = json.dumps(receipt)

    class Failing(Backend):
        async def execute_call(self, invocation):
            return body

    runtime = runtime_for(chat, await chat._runtime_config.snapshot())
    await chat.runtime.runner.run((ChatMessage("user", "go"),), runtime, Failing())
    assert [m.content for m in provider.requests[1].messages if m.role == "tool"] == [body]


async def test_host_exceptions_go_to_their_owner_not_into_a_tool_result(database):
    # Cancellation, lease and storage failures are not model-visible receipts.
    responses = iter([ChatResponse("", 0, tool_calls=(call("w", "send_message"),))])
    provider = FakeLLMProvider(lambda _request: next(responses))
    chat = build_harness(database, make_settings(database.url), provider).processor._chat

    class Broken(Backend):
        async def execute_call(self, invocation):
            raise ConnectionResetError("storage")

    runtime = runtime_for(chat, await chat._runtime_config.snapshot())
    # A sequential write propagates the original exception to the runner's owner.
    with pytest.raises(ConnectionResetError, match="storage"):
        await chat.runtime.runner.run((ChatMessage("user", "go"),), runtime, Broken())
    assert len(provider.requests) == 1
