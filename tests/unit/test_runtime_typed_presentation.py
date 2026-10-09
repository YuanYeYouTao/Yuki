"""Display bytes cannot authorize reuse, completion, or pending execution state."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from qq_ai_bot.agent_core import End, ToolBatchOutcome
from qq_ai_bot.capabilities.coordinator import CoordinatedToolResult
from qq_ai_bot.capabilities.results import ToolExecutionResult
from qq_ai_bot.domain.messages import ChatMessage, ChatResponse, ToolCall, ToolFunction
from qq_ai_bot.runtime.effect_outcomes import (
    ResultCapture,
    current_result_capture,
    execution_evidence,
)
from qq_ai_bot.services.agent_runner import AgentRunner
from qq_ai_bot.services.turn_execution import TurnExecution


class Backend:
    def __init__(self, outcome, display):
        self.outcome, self.display = outcome, display
        self.calls = []

    def counts_toward_limit(self, *_):
        return True

    def parallel_safe(self, *_):
        return False

    def is_side_effecting(self, name, *_):
        return name != "read"

    async def execute_call(self, invocation):
        self.calls.append(invocation.call.function.name)
        capture = current_result_capture.get()
        assert capture is not None
        capture.outcome = self.outcome
        return self.display


def call(key, name="read", args="{}"):
    return ToolCall(key, ToolFunction(name, args))


async def batch(runner, backend, cache, calls):
    return await runner._execute_tool_batch_impl(
        calls,
        backend,
        SimpleNamespace(work_control=None, script_api=None),
        remaining_calls=10,
        max_parallel_calls=1,
        reusable_results=cache,
        cacheable_names=frozenset({"read"}),
        declared_names=frozenset({"read", "write", "memory_change", "send_message"}),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome,display,cached",
    [
        (ToolExecutionResult(ok=True), '{"ok":false}', True),
        (ToolExecutionResult(ok=False), '{"ok":true}', False),
        (ToolExecutionResult(ok=True, retryable=True), '{"ok":true}', False),
        (ToolExecutionResult(ok=True, data={"pending": True}), '{"ok":true}', False),
        (ToolExecutionResult(ok=True, uncertain=True), '{"ok":true}', False),
    ],
)
async def test_cache_eligibility_uses_original_typed_fact(outcome, display, cached):
    runner, cache = AgentRunner(None, None), {}
    backend = Backend(outcome, display)
    first = await batch(runner, backend, cache, (call("original"), call("alias")))
    second = await batch(runner, backend, cache, (call("later"),))
    assert first.evidence["alias"] == first.evidence["original"]
    assert first.reused_count == 1
    assert second.reused_count == int(cached)
    assert len(backend.calls) == (1 if cached else 2)
    assert second.calls[0][1] == display
    assert second.evidence["later"]["ok"] is outcome.ok


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "committed,display,invalidates",
    [
        (True, '{"ok":false,"mutation_committed":false}', True),
        (False, '{"ok":true,"mutation_committed":true}', False),
        (None, '{"ok":false,"mutation_committed":false}', True),
    ],
)
async def test_cache_invalidation_uses_commit_fact_even_on_failed_display(
    committed,
    display,
    invalidates,
):
    runner, cache = AgentRunner(None, None), {}
    await batch(runner, Backend(ToolExecutionResult(ok=True), "read"), cache, (call("read"),))
    assert cache
    await batch(
        runner,
        Backend(ToolExecutionResult(ok=False, mutation_committed=committed), display),
        cache,
        (call("write", "write"),),
    )
    assert bool(cache) is not invalidates


@pytest.mark.asyncio
@pytest.mark.parametrize("ok", [False, True])
async def test_multiple_memory_changes_keep_delivery_observation_boundary(ok):
    backend = Backend(ToolExecutionResult(ok=ok, mutation_committed=ok), "display")
    runner = AgentRunner(None, None)
    result = await batch(
        runner,
        backend,
        {},
        (
            call("memory", "memory_change"),
            call("second-memory", "memory_change", '{"fact":"different"}'),
            call("write", "write"),
            call("delivery", "send_message"),
        ),
    )
    assert backend.calls == ["memory_change", "memory_change", "write"]
    assert result.executed_count == 3
    assert result.calls[-1][2] is False
    assert json.loads(result.calls[-1][1])["error"] == "delivery_requires_observed_result"
    # A later model batch can author the message after observing the real result.
    later = await batch(runner, backend, {}, (call("observed-delivery", "send_message"),))
    assert later.executed_count == 1
    assert backend.calls[-1] == "send_message"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path", ["business_missing_api", "business_undeclared", "control_missing_api"]
)
async def test_code_host_pre_dispatch_refusal_publishes_typed_fact(path):
    runtime = SimpleNamespace(
        work_control=SimpleNamespace(lease=SimpleNamespace(work_id="original")),
        script_api=SimpleNamespace(schemas={}) if path == "business_undeclared" else None,
        runtime_config=SimpleNamespace(agent=SimpleNamespace(tool_result_max_characters=12000)),
        max_tool_calls=10,
    )
    tools = SimpleNamespace(archive_code_result=None)
    host = AgentRunner(None, None)._code_host(
        tools,
        runtime,
        declared_names=frozenset(),
        max_parallel_calls=1,
    )
    capture = ResultCapture("original", "original-call")
    token = current_result_capture.set(capture)
    try:
        if path == "control_missing_api":
            display, executed = await host.execute_control(
                call("original-call", "task_control"), "original-key"
            )
            assert not executed
        else:
            display = await host.execute_business(
                SimpleNamespace(call=call("original-call", "write")), True
            )
    finally:
        current_result_capture.reset(token)
    assert capture.outcome is not None
    assert not capture.outcome.ok
    assert capture.outcome.error_code == "tool_not_declared"
    assert capture.outcome.data == {"executed": False}
    assert capture.outcome.mutation_committed is False
    assert json.loads(display)["executed"] is False


def turn_for(outcome, display, *, completed=False, accepted=None):
    name = "task_control" if completed else "read"
    tool_call = call("call", name, '{"action":"complete"}' if completed else "{}")
    session = SimpleNamespace(progress={}, save=AsyncMock())
    control = SimpleNamespace(
        session=session,
        ending=None,
        accepted=accepted,
        # Only the persisted accepted decision ends the activation; the
        # display returned to the model is never consulted.
        accepted_ending=lambda *_: "completed" if accepted else None,
        source={"delivery_contract": "return_to_caller"} if completed else {},
        handoff_work_id=None,
        current=None,
        lease=SimpleNamespace(work_id="work"),
    )
    runtime = SimpleNamespace(
        work_control=control,
        origin=SimpleNamespace(value="user_message"),
        visible_event_ids=(),
    )
    turn = TurnExecution(
        AgentRunner(None, None), (ChatMessage("assistant", tool_calls=(tool_call,)),), runtime, None
    )
    fact = execution_evidence(outcome, tool=name, side_effecting=completed)
    turn.state.coordinated = CoordinatedToolResult(
        ((tool_call, display, True),),
        1,
        evidence={tool_call.id: fact},
    )
    return turn, session


@pytest.mark.asyncio
@pytest.mark.parametrize("pending", [False, True])
async def test_pending_repeat_guard_ignores_opposite_display(pending):
    turn, session = turn_for(
        ToolExecutionResult(ok=True, data={"pending": pending}),
        json.dumps({"ok": True, "data": {"pending": not pending}}),
    )
    for index in range(2):
        await turn.finish_tool_turn(index, ChatResponse("", 0), ToolBatchOutcome(()))
    assert session.progress["repeats"] == (0 if pending else 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("accepted", [False, True])
async def test_accepted_completion_ends_on_persisted_decision_not_display(accepted):
    decision = (
        {"action": "complete", "call_key": "call", "result": "internal result"}
        if accepted
        else None
    )
    turn, session = turn_for(
        ToolExecutionResult(ok=accepted),
        json.dumps({"ok": not accepted, "ending_proposed": "completed"}),
        completed=True,
        accepted=decision,
    )
    outcome = await turn.finish_tool_turn(0, ChatResponse("", 0), ToolBatchOutcome(()))
    assert isinstance(outcome, End) is accepted
    assert "caller_completion_pending_result" not in session.progress
    if accepted:
        # The activation ends with the accepted internal result; it is
        # returned to its owner and never delivered by itself.
        assert outcome.value.text == "internal result"
        assert outcome.value.suppress_delivery is True
        assert outcome.value.work_state == "completed"
        assert outcome.value.model_requests == 1
