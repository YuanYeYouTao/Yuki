# Portions ported from Pi (https://github.com/earendil-works/pi) at
# 200387122ca450d6387f033949423114a270b96c, packages/agent/src/agent-loop.ts:
# ``runAgentLoop`` / ``runAgentLoopContinue`` (L102-L151), ``runLoop``
# (L163-L327), ``failToolCallsFromTruncatedMessage`` (L478-L503) and
# ``shouldTerminateToolBatch`` (L689-L691).
# MIT License, Copyright (c) 2025 Mario Zechner.
"""The single production model loop.

Semantics kept from Pi: one turn is one complete assistant response plus its
tool results; tool results always lead to another request; steering input is
taken before each request; a truncated ("length") response fails every tool
call in-band instead of executing it; ``agent_end`` is always the last event.

Yuki-specific owners stay outside this module (see ``model_boundary``): it
imports no platform, database, Provider or Work code.
"""

from __future__ import annotations

import json

from qq_ai_bot.agent_core.events import EventStream
from qq_ai_bot.agent_core.model_boundary import (
    RETRY,
    STOP,
    InvocationBoundary,
    LoopSignal,
    ModelBoundary,
    TurnSettlement,
)
from qq_ai_bot.agent_core.types import (
    AgentEvent,
    End,
    StopReason,
    ToolCallOutcome,
    TurnDecision,
    stop_reason,
)
from qq_ai_bot.domain.messages import ChatResponse

# The Host receipt for a call that was never executed because its arguments may
# be truncated. Field order is part of the existing wire contract.
TRUNCATED_CALL_RECEIPT = json.dumps(
    {
        "ok": False,
        "error": "provider_response_incomplete",
        "executed": False,
        "mutation_committed": False,
    }
)


def fail_truncated_calls(
    response: ChatResponse, events: EventStream
) -> tuple[ToolCallOutcome, ...]:
    """Pi ``failToolCallsFromTruncatedMessage``: nothing is dispatched."""
    outcomes = []
    for call in response.tool_calls:
        events.emit(AgentEvent("tool_execution_start", call=call))
        outcome = ToolCallOutcome(call, TRUNCATED_CALL_RECEIPT, executed=False)
        events.emit(
            AgentEvent("tool_execution_end", call=call, result=outcome.result, executed=False)
        )
        outcomes.append(outcome)
    return tuple(outcomes)


async def run_agent_loop(
    *,
    max_requests: int,
    model: ModelBoundary,
    invocation: InvocationBoundary,
    settlement: TurnSettlement,
    events: EventStream | None = None,
) -> object:
    """Pi ``runAgentLoop``/``runAgentLoopContinue`` + ``runLoop``.

    Pi's continue entry differs only in not adding a prompt; Yuki's context is
    already composed (or restored from the Work journal) by the model boundary,
    so both entries collapse into this one. The return value is the settlement's
    typed result for whichever decision ended the run.
    """
    stream = events if events is not None else EventStream()
    stream.emit(AgentEvent("agent_start"))
    try:
        # Pi has an unbounded ``while (true)``. Yuki bounds the run by the
        # request budget; the boundary may stop earlier on its own limits.
        for index in range(max_requests):
            if await model.begin(index) is STOP:
                break
            stream.emit(AgentEvent("turn_start", index=index))
            # Pi: pending steering messages are appended before the request.
            steered = await model.steer(index)
            if steered is not None:
                return steered.value
            result = await model.request(index)
            if result is RETRY:
                stream.emit(AgentEvent("turn_end", index=index))
                continue
            if result is STOP:
                break
            if isinstance(result, End):
                return result.value
            assert isinstance(result, ChatResponse)
            response = result
            stream.emit(AgentEvent("message_start", index=index, response=response))
            stream.emit(AgentEvent("message_end", index=index, response=response))
            reason = stop_reason(response)
            decision: TurnDecision | LoopSignal
            outcomes: tuple[ToolCallOutcome, ...] = ()
            if reason is StopReason.LENGTH:
                outcomes = fail_truncated_calls(response, stream)
                decision = await settlement.settle_truncated(index, response, outcomes)
            elif reason is StopReason.STOP:
                decision = await settlement.settle_final(index, response)
            else:
                stopped = await settlement.stop_before_tools(index, response)
                if stopped is not None:
                    decision = stopped
                else:
                    for call in response.tool_calls:
                        stream.emit(AgentEvent("tool_execution_start", index=index, call=call))
                    batch = await invocation.execute_tools(index, response)
                    outcomes = batch.outcomes
                    for outcome in outcomes:
                        stream.emit(
                            AgentEvent(
                                "tool_execution_end",
                                index=index,
                                call=outcome.call,
                                result=outcome.result,
                                executed=outcome.executed,
                            )
                        )
                    # Pi ``shouldTerminateToolBatch``: an all-terminate batch
                    # still settles, but does not force another request.
                    decision = await settlement.finish_tool_turn(index, response, batch)
                    if batch.terminate and not isinstance(decision, End):
                        decision = STOP
            stream.emit(
                AgentEvent(
                    "turn_end",
                    index=index,
                    response=response,
                    outcomes=outcomes,
                    stop_reason=reason,
                )
            )
            if decision is STOP:
                break
            if isinstance(decision, End):
                return decision.value
            # ``Continue``: tool results, recovery feedback or follow-up input
            # are already in the context; Pi always runs one more request.
        return await settlement.exhausted()
    finally:
        # Synchronous: also runs when the task is cancelled mid-turn.
        stream.emit(AgentEvent("agent_end"))
