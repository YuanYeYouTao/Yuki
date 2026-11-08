"""The three narrow interfaces the loop drives, plus synthetic frame assembly.

Yuki has exactly three owners: the model boundary (request
preparation, inputs, dispatch), the invocation boundary (one tool batch through
the original InvocationService) and turn settlement. No hook registry exists.
"""

from __future__ import annotations

from typing import Protocol

from yuki.agent_core.types import (
    End,
    ToolBatchOutcome,
    ToolCallOutcome,
    TurnDecision,
)
from yuki.domain.messages import ChatResponse

RequestResult = ChatResponse | End


class ModelBoundary(Protocol):
    async def begin(self, index: int) -> None:
        """Per-request admission (public observation boundary)."""

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
    ) -> TurnDecision: ...
