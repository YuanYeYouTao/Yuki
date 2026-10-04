# Portions ported from Pi (https://github.com/earendil-works/pi) at
# 200387122ca450d6387f033949423114a270b96c, packages/agent/src/agent-loop.ts
# (``streamAssistantResponse``, ``AgentLoopConfig`` request/turn callbacks).
# MIT License, Copyright (c) 2025 Mario Zechner.
"""The three narrow interfaces the loop drives, plus synthetic frame assembly.

Pi exposes an open hook bag (``prepareNextTurn``, ``beforeToolCall``, ...).
Yuki deliberately has exactly three owners: the model boundary (request
preparation, inputs, dispatch), the invocation boundary (one tool batch through
the original InvocationService) and turn settlement. No hook registry exists.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, replace
from typing import Literal, Protocol

from qq_ai_bot.agent_core.types import (
    AgentEvent,
    End,
    ToolBatchOutcome,
    ToolCallOutcome,
    TurnDecision,
)
from qq_ai_bot.domain.messages import ChatResponse, ModelResponseStatus, ToolCall, ToolFunction


class LoopSignal:
    __slots__ = ("name",)

    def __init__(self, name: str) -> None:
        self.name = name

    def __repr__(self) -> str:
        return f"<{self.name}>"


# The boundary consumed this request index without a model response to act on
# (for example a bounded empty-response retry): advance to the next index.
RETRY = LoopSignal("retry")
# Request budget or segment limit reached: settle as exhausted.
STOP = LoopSignal("stop")

RequestResult = ChatResponse | End | LoopSignal


class ModelBoundary(Protocol):
    async def begin(self, index: int) -> LoopSignal | None:
        """Per-request admission (budgets, public observation boundary)."""

    async def steer(self, index: int) -> End | None:
        """Pi ``getSteeringMessages``: durable Work inputs before the next request."""

    async def request(self, index: int) -> RequestResult:
        """Pi ``prepareRequest`` + ``streamAssistantResponse``: one complete response."""


class InvocationBoundary(Protocol):
    async def execute_tools(self, index: int, response: ChatResponse) -> ToolBatchOutcome:
        """One batch through the original coordinator, receipts in call order."""


class TurnSettlement(Protocol):
    async def settle_truncated(
        self, index: int, response: ChatResponse, outcomes: tuple[ToolCallOutcome, ...]
    ) -> TurnDecision: ...

    async def settle_final(self, index: int, response: ChatResponse) -> TurnDecision: ...

    async def stop_before_tools(self, index: int, response: ChatResponse) -> End | None: ...

    async def finish_tool_turn(
        self, index: int, response: ChatResponse, batch: ToolBatchOutcome
    ) -> TurnDecision | LoopSignal: ...

    async def exhausted(self) -> object: ...


@dataclass(frozen=True, slots=True)
class Callbacks:
    """Bind the three boundaries from closures owned by one caller (the Runner).

    This is a fixed set of named responsibilities, not an extensible registry.
    """

    begin: Callable[[int], Awaitable[LoopSignal | None]]
    steer: Callable[[int], Awaitable[End | None]]
    request: Callable[[int], Awaitable[RequestResult]]
    execute_tools: Callable[[int, ChatResponse], Awaitable[ToolBatchOutcome]]
    settle_truncated: Callable[
        [int, ChatResponse, tuple[ToolCallOutcome, ...]], Awaitable[TurnDecision]
    ]
    settle_final: Callable[[int, ChatResponse], Awaitable[TurnDecision]]
    stop_before_tools: Callable[[int, ChatResponse], Awaitable[End | None]]
    finish_tool_turn: Callable[
        [int, ChatResponse, ToolBatchOutcome], Awaitable[TurnDecision | LoopSignal]
    ]
    exhausted: Callable[[], Awaitable[object]]


# --- Synthetic frames (tests and boundary contracts only) -------------------
#
# Yuki providers currently return complete responses; this assembly exists so
# partial-response invariants are executable without claiming vendor streaming.


@dataclass(frozen=True, slots=True)
class Frame:
    """Pi ``AssistantMessageEvent`` subset, as an immutable partial snapshot."""

    type: Literal["start", "text_delta", "toolcall_delta", "done", "error"]
    text: str = ""
    call_id: str = ""
    name: str = ""
    arguments: str = ""


async def collect_response(
    frames: AsyncIterator[Frame], emit: Callable[[AgentEvent], None]
) -> ChatResponse:
    """Port of ``streamAssistantResponse``: only a ``done`` frame completes.

    A stream that ends early or with ``error`` yields an INCOMPLETE response,
    which the loop never executes. Emitted payloads are frozen copies, so a
    listener never sees a later partial state.
    """
    partial = ChatResponse("", 0, status=ModelResponseStatus.INCOMPLETE)
    arguments: dict[str, list[str]] = {}
    names: dict[str, str] = {}
    started = False
    async for frame in frames:
        if frame.type == "start":
            started = True
            emit(AgentEvent("message_start", response=partial))
            continue
        if not started:
            raise ValueError("frame_before_start")
        if frame.type == "text_delta":
            partial = replace(partial, content=partial.content + frame.text)
        elif frame.type == "toolcall_delta":
            names.setdefault(frame.call_id, frame.name)
            arguments.setdefault(frame.call_id, []).append(frame.arguments)
        calls = tuple(
            ToolCall(call_id, ToolFunction(names[call_id], "".join(parts)))
            for call_id, parts in arguments.items()
        )
        partial = replace(partial, tool_calls=calls)
        if frame.type in {"done", "error"}:
            if frame.type == "done":
                partial = replace(partial, status=ModelResponseStatus.COMPLETED)
            else:
                partial = replace(partial, incomplete_reason="stream_error")
            break
        emit(AgentEvent("message_start", response=partial))
    else:
        partial = replace(partial, incomplete_reason="stream_ended")
    emit(AgentEvent("message_end", response=partial))
    return partial
