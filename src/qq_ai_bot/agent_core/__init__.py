"""Yuki's model loop and typed execution boundaries.

Design references are recorded in docs/architecture/pi-port-provenance.md.
"""

from qq_ai_bot.agent_core.loop import TRUNCATED_CALL_RECEIPT, run_agent_loop
from qq_ai_bot.agent_core.model_boundary import STOP
from qq_ai_bot.agent_core.types import (
    Continue,
    End,
    StopReason,
    ToolBatchOutcome,
    ToolCallOutcome,
    TurnDecision,
)

__all__ = [
    "STOP",
    "TRUNCATED_CALL_RECEIPT",
    "Continue",
    "End",
    "StopReason",
    "ToolBatchOutcome",
    "ToolCallOutcome",
    "TurnDecision",
    "run_agent_loop",
]
