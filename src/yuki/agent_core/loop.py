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

from yuki.agent_core.model_boundary import (
    InvocationBoundary,
    ModelBoundary,
    TurnSettlement,
)
from yuki.agent_core.types import (
    End,
    StopReason,
    ToolCallOutcome,
    TurnDecision,
    stop_reason,
)
from yuki.domain.messages import ChatResponse

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
    model: ModelBoundary,
    invocation: InvocationBoundary,
    settlement: TurnSettlement,
) -> object:
    """Drive Yuki's request, invocation and settlement owners until settlement ends.

    The model boundary has already composed or restored the request context.
    Ordinary and resumed execution therefore share this entry. The return
    value belongs to the settlement that ends the activation; explicit root
    Work budgets are enforced where each request or tool is admitted.
    """
    index = 0
    while True:
        await model.begin(index)
        # Consume accepted inputs before dispatching the next request.
        steered = await model.steer(index)
        if steered is not None:
            return steered.value
        result = await model.request(index)
        if isinstance(result, End):
            return result.value
        response = result
        reason = stop_reason(response)
        decision: TurnDecision
        if reason is StopReason.LENGTH:
            decision = await settlement.settle_truncated(
                index, response, fail_truncated_calls(response)
            )
        elif reason is StopReason.STOP:
            decision = await settlement.settle_final(index, response)
        else:
            batch = await invocation.execute_tools(index, response)
            decision = await settlement.finish_tool_turn(index, response, batch)
        if isinstance(decision, End):
            return decision.value
        # ``Continue``: tool results, recovery feedback or follow-up input
        # are already in the context for the next request.
        index += 1
