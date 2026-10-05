"""The three narrow interfaces the loop drives, plus synthetic frame assembly.

Yuki has exactly three owners: the model boundary (request
preparation, inputs, dispatch), the invocation boundary (one tool batch through
the original InvocationService) and turn settlement. No hook registry exists.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
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
        """Consume durable Work inputs before the next request."""

    async def request(self, index: int) -> RequestResult:
        """Prepare and dispatch one request, returning its complete response."""


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


# --- Synthetic frames (tests and boundary contracts only) -------------------
#
# Yuki providers currently return complete responses; this assembly exists so
# partial-response invariants are executable without claiming vendor streaming.


@dataclass(frozen=True, slots=True)
class Frame:
    """An immutable synthetic response fragment for boundary tests."""

    type: Literal["start", "text_delta", "toolcall_delta", "done", "error"]
    text: str = ""
    call_id: str = ""
    name: str = ""
    arguments: str = ""


async def collect_response(
    frames: AsyncIterator[Frame], emit: Callable[[AgentEvent], None]
) -> ChatResponse:
    """Assemble synthetic fragments; only a ``done`` frame completes a response.

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
