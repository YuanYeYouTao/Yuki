"""Yuki's immutable execution records over its own message model.

Provider messages retain their original types so private signatures and opaque
continuations are never rewritten into another JSON shape.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Literal

from qq_ai_bot.domain.messages import ChatMessage, ChatResponse, ModelResponseStatus, ToolCall


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


@dataclass(frozen=True, slots=True)
class AgentEvent:
    """One frozen record per Yuki execution transition.

    Payloads are snapshots (tuples of frozen messages) so a listener can never
    observe a later mutation of loop state; see ``events.EventStream``.
    """

    type: Literal[
        "agent_start",
        "agent_end",
        "turn_start",
        "turn_end",
        "message_start",
        "message_end",
        "tool_execution_start",
        "tool_execution_end",
    ]
    index: int = 0
    message: ChatMessage | None = None
    response: ChatResponse | None = None
    call: ToolCall | None = None
    result: str | None = None
    executed: bool | None = None
    outcomes: tuple[ToolCallOutcome, ...] = field(default=())
    stop_reason: StopReason | None = None
