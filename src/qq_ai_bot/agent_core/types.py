# Portions ported from Pi (https://github.com/earendil-works/pi) at
# 200387122ca450d6387f033949423114a270b96c, packages/agent/src/types.ts.
# MIT License, Copyright (c) 2025 Mario Zechner. See NOTICE in
# docs/architecture/pi-port-provenance.md for the full license text.
"""Adopted Pi agent types, re-expressed over Yuki's own message model.

Source symbols: ``AgentEvent``, ``AgentTurnDecision``, ``AgentToolCallOutcome``.
Pi's ``AgentMessage``/``AssistantMessage`` are not copied: Yuki keeps its
provider types (``ChatResponse``/``ToolCall``) so private signatures and opaque
continuations are never rewritten into another JSON shape.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Literal

from qq_ai_bot.domain.messages import ChatMessage, ChatResponse, ModelResponseStatus, ToolCall


class StopReason(StrEnum):
    """Pi ``AssistantMessage.stopReason`` subset that changes loop control."""

    STOP = "stop"
    TOOL_USE = "toolUse"
    # Pi "length": output cut off, so every tool call may carry truncated arguments.
    LENGTH = "length"


def stop_reason(response: ChatResponse) -> StopReason:
    if response.status is ModelResponseStatus.INCOMPLETE:
        return StopReason.LENGTH
    return StopReason.TOOL_USE if response.tool_calls else StopReason.STOP


@dataclass(frozen=True, slots=True)
class ToolCallOutcome:
    """Pi ``AgentToolCallOutcome``: the ordered receipt for one original call.

    ``result`` is the original typed receipt string; errors are never flattened
    into an exception (Pi ``createErrorToolResult`` keeps them in-band too).
    """

    call: ToolCall
    result: str
    executed: bool


@dataclass(frozen=True, slots=True)
class ToolBatchOutcome:
    """Pi ``ExecutedToolCallBatch`` plus Yuki's Host execution count."""

    outcomes: tuple[ToolCallOutcome, ...]
    executed_count: int = 0
    reused_count: int = 0
    # Pi ``shouldTerminateToolBatch``: every result asked the loop to stop.
    terminate: bool = False


@dataclass(frozen=True, slots=True)
class Continue:
    """Pi ``{action: "continue"}``: run one more provider request."""

    reason: str = ""


@dataclass(frozen=True, slots=True)
class End:
    """Pi ``{action: "end"}`` carrying the settlement's typed final value."""

    value: object = None


TurnDecision = Continue | End


@dataclass(frozen=True, slots=True)
class AgentEvent:
    """Pi ``AgentEvent`` union, as one frozen record per transition.

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
