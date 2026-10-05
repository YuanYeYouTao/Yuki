"""The single production model loop.

One turn is one complete assistant response plus its tool results. Pending
inputs are consumed before each request, incomplete calls receive unexecuted
receipts, and every exit emits ``agent_end``. These are Yuki's tested execution
contracts; the design reference is recorded separately from dependencies.

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
    """Produce original-call receipts without dispatching incomplete arguments."""
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
    """Drive Yuki's request, invocation and settlement owners within a budget.

    The model boundary has already composed or restored the request context.
    Ordinary and resumed execution therefore share this entry. The return
    value belongs to the settlement that ends the activation.
    """
    stream = events if events is not None else EventStream()
    stream.emit(AgentEvent("agent_start"))
    try:
        # Admission may stop before this activation's request budget is spent.
        for index in range(max_requests):
            if await model.begin(index) is STOP:
                break
            stream.emit(AgentEvent("turn_start", index=index))
            # Consume accepted inputs before dispatching the next request.
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
                    # A terminating batch settles its receipts before exiting.
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
            # are already in the context for the next request.
        return await settlement.exhausted()
    finally:
        # Synchronous: also runs when the task is cancelled mid-turn.
        stream.emit(AgentEvent("agent_end"))
