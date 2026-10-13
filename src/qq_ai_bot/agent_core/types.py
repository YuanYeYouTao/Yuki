"""Yuki's immutable execution records over its own message model.

Provider messages retain their original types so private signatures and opaque
continuations are never rewritten into another JSON shape.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from qq_ai_bot.domain.messages import ChatResponse, ModelResponseStatus, ToolCall


class StopReason(StrEnum):
    """Response states that determine Yuki's next execution step."""

    STOP = "stop"
    TOOL_USE = "toolUse"
    # Output cut off: every call may carry incomplete arguments.
    LENGTH = "length"


def stop_reason(response: ChatResponse) -> StopReason:
    if response.status is ModelResponseStatus.INCOMPLETE:
        return StopReason.LENGTH
    return StopReason.TOOL_USE if response.tool_calls else StopReason.STOP


@dataclass(frozen=True, slots=True)
class ToolCallOutcome:
    """The ordered receipt for one original Yuki tool call.

    ``result`` is the original typed receipt string; errors are never flattened
    into an exception.
    """

    call: ToolCall
    result: str
    executed: bool


@dataclass(frozen=True, slots=True)
class ToolBatchOutcome:
    """Ordered Host receipts and admission counts for one tool batch."""

    outcomes: tuple[ToolCallOutcome, ...]
    executed_count: int = 0
    reused_count: int = 0
    # Every result asked the loop to stop.
    terminate: bool = False


@dataclass(frozen=True, slots=True)
class Continue:
    """Run one more provider request with the settled context."""

    reason: str = ""


@dataclass(frozen=True, slots=True)
class End:
    """End the activation with the settlement's typed final value."""

    value: object = None


TurnDecision = Continue | End
