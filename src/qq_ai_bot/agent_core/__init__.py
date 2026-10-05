"""Yuki's model loop and typed execution boundaries.

Design references are recorded in docs/architecture/pi-port-provenance.md.
"""

from qq_ai_bot.agent_core.events import EventStream
from qq_ai_bot.agent_core.loop import TRUNCATED_CALL_RECEIPT, run_agent_loop
from qq_ai_bot.agent_core.model_boundary import RETRY, STOP
from qq_ai_bot.agent_core.state import AgentState, reduce
from qq_ai_bot.agent_core.types import (
    AgentEvent,
    Continue,
    End,
    StopReason,
    ToolBatchOutcome,
    ToolCallOutcome,
    TurnDecision,
)

__all__ = [
    "RETRY",
    "STOP",
    "TRUNCATED_CALL_RECEIPT",
    "AgentEvent",
    "AgentState",
    "Continue",
    "End",
    "EventStream",
    "StopReason",
    "ToolBatchOutcome",
    "ToolCallOutcome",
    "TurnDecision",
    "reduce",
    "run_agent_loop",
]
