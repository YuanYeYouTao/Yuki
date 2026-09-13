"""Authority-bound invocation context for the unified Tool Kernel."""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class ToolInvocationContext:
    """Runtime values that providers may consume but a model can never supply."""

    runtime: Any
    call_id: str = ""
    conversation_key: str = ""
    actor_user_id: str = ""
    trigger_message_id: str = ""
    execution_id: str = ""
    provider_metadata: dict[str, Any] | None = None

    @property
    def execution_key(self) -> str:
        identity = self.execution_id or getattr(self.runtime, "effective_execution_id", None)
        identity = identity or getattr(self.runtime, "execution_id", None)
        if not identity:
            raise ValueError("missing_internal_execution_anchor")
        return str(identity)


current_invocation: ContextVar[ToolInvocationContext | None] = ContextVar(
    "tool_invocation", default=None
)
