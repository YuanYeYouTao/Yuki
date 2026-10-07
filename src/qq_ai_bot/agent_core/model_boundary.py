"""The three narrow interfaces the loop drives, plus synthetic frame assembly.

Yuki has exactly three owners: the model boundary (request
preparation, inputs, dispatch), the invocation boundary (one tool batch through
the original InvocationService) and turn settlement. No hook registry exists.
"""

from __future__ import annotations

from typing import Protocol

from qq_ai_bot.agent_core.types import (
    End,
    ToolBatchOutcome,
    ToolCallOutcome,
    TurnDecision,
)
from qq_ai_bot.domain.messages import ChatResponse


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

    async def finish_tool_turn(
        self, index: int, response: ChatResponse, batch: ToolBatchOutcome
    ) -> TurnDecision | LoopSignal: ...

    async def exhausted(self) -> object: ...
