"""Execution bindings used by all Tool Kernel providers."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import Protocol

from qq_ai_bot.capabilities.invocation import ToolInvocationContext
from qq_ai_bot.capabilities.results import ToolExecutionResult


class ToolBinding(Protocol):
    async def invoke(
        self,
        arguments: dict[str, object],
        context: ToolInvocationContext,
    ) -> ToolExecutionResult: ...


InProcessHandler = Callable[
    [dict[str, object], ToolInvocationContext],
    Awaitable[ToolExecutionResult],
]


@dataclass(frozen=True, slots=True)
class InProcessToolBinding:
    """Bind an existing in-process handler without exposing its service type."""

    provider_id: str
    tool_name: str
    handler: InProcessHandler

    async def invoke(
        self,
        arguments: dict[str, object],
        context: ToolInvocationContext,
    ) -> ToolExecutionResult:
        value = await self.handler(arguments, context)
        if not isinstance(value, ToolExecutionResult):
            raise TypeError("tool handler must return ToolExecutionResult")
        return replace(value, provider_id=self.provider_id, tool_name=self.tool_name)
