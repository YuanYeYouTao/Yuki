"""The single production model loop.

One turn is one complete assistant response plus its tool results. Pending
inputs are consumed before each request, incomplete calls receive unexecuted
receipts. These are Yuki's tested execution
contracts; the design reference is recorded separately from dependencies.

Yuki-specific owners stay outside this module (see ``model_boundary``): it
imports no platform, database, Provider or Work code.
"""

from __future__ import annotations

import json
from itertools import count

from qq_ai_bot.agent_core.model_boundary import (
    STOP,
    InvocationBoundary,
    LoopSignal,
    ModelBoundary,
    TurnSettlement,
)
from qq_ai_bot.agent_core.types import (
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


def fail_truncated_calls(response: ChatResponse) -> tuple[ToolCallOutcome, ...]:
    """Produce original-call receipts without dispatching incomplete arguments."""
    outcomes = []
    for call in response.tool_calls:
        outcome = ToolCallOutcome(call, TRUNCATED_CALL_RECEIPT, executed=False)
        outcomes.append(outcome)
    return tuple(outcomes)


async def run_agent_loop(
    *,
    max_requests: int | None,
    model: ModelBoundary,
    invocation: InvocationBoundary,
    settlement: TurnSettlement,
) -> object:
    """Drive Yuki's request, invocation and settlement owners within a budget.

    The model boundary has already composed or restored the request context.
    Ordinary and resumed execution therefore share this entry. The return
    value belongs to the settlement that ends the activation.
    """
    # Admission may stop before this activation's request budget is spent.
    for index in count():
        if max_requests is not None and index >= max_requests:
            break
        if await model.begin(index) is STOP:
            break
        # Consume accepted inputs before dispatching the next request.
        steered = await model.steer(index)
        if steered is not None:
            return steered.value
        result = await model.request(index)
        if result is STOP:
            break
        if isinstance(result, End):
            return result.value
        assert isinstance(result, ChatResponse)
        response = result
        reason = stop_reason(response)
        decision: TurnDecision | LoopSignal
        outcomes: tuple[ToolCallOutcome, ...] = ()
        if reason is StopReason.LENGTH:
            outcomes = fail_truncated_calls(response)
            decision = await settlement.settle_truncated(index, response, outcomes)
        elif reason is StopReason.STOP:
            decision = await settlement.settle_final(index, response)
        else:
            batch = await invocation.execute_tools(index, response)
            outcomes = batch.outcomes
            # A terminating batch settles its receipts before exiting.
            decision = await settlement.finish_tool_turn(index, response, batch)
            if batch.terminate and not isinstance(decision, End):
                decision = STOP
        if decision is STOP:
            break
        if isinstance(decision, End):
            return decision.value
        # ``Continue``: tool results, recovery feedback or follow-up input
        # are already in the context for the next request.
    return await settlement.exhausted()
