"""Low-cardinality in-process Tool Kernel metrics."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field


@dataclass(slots=True)
class ToolKernelMetrics:
    invocations: Counter[tuple[str, str, bool]] = field(default_factory=Counter)
    tool_enabled_turns: int = 0
    automatic_memory_read_tool_calls: int = 0
    mutation_locator_read_fallbacks: int = 0

    def record_invocation(self, provider_id: str, tool_name: str, ok: bool) -> None:
        self.invocations[(provider_id, tool_name, ok)] += 1

    def record_tool_enabled_turn(self) -> None:
        """Count one tool-capable turn without retaining conversation identity."""

        self.tool_enabled_turns += 1

    def record_automatic_memory_read_tool_call(self, *, locator_fallback: bool) -> None:
        self.automatic_memory_read_tool_calls += 1
        if locator_fallback:
            self.mutation_locator_read_fallbacks += 1
