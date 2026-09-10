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
    provider_metadata: dict[str, Any] | None = None


current_invocation: ContextVar[ToolInvocationContext | None] = ContextVar(
    "tool_invocation", default=None
)
