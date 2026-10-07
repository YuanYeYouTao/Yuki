"""Scripted core boundary fixture; absent from the production executable graph."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from qq_ai_bot.agent_core.model_boundary import LoopSignal, RequestResult
from qq_ai_bot.agent_core.types import End, ToolBatchOutcome, ToolCallOutcome, TurnDecision
from qq_ai_bot.domain.messages import ChatResponse


@dataclass(frozen=True, slots=True)
class Callbacks:
    """Bind scripted boundaries in core tests only; production uses typed owners.

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
