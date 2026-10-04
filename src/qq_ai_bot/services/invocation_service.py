"""One original-call execution boundary for direct and composed tool calls."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

from qq_ai_bot.capabilities.invocation import Invocation
from qq_ai_bot.domain.messages import ToolCall


class InvocationBackend(Protocol):
    async def execute_call(self, invocation: Invocation) -> str: ...


@dataclass(frozen=True, slots=True)
class BatchPlan:
    """Response-local identity validation, independent of completion order."""

    calls: tuple[ToolCall, ...]
    conflicting_ids: frozenset[str]

    @classmethod
    def prepare(cls, calls: tuple[ToolCall, ...]) -> BatchPlan:
        seen: set[str] = set()
        conflicts: set[str] = set()
        for call in calls:
            if call.id in seen:
                conflicts.add(call.id)
            seen.add(call.id)
        return cls(calls, frozenset(conflicts))


class InvocationService:
    """Reuse WorkSession's original effect and budget owner, outside the binding."""

    async def invoke(
        self,
        invocation: Invocation,
        execute: Callable[[], Awaitable[str]],
        *,
        side_effecting: bool,
    ) -> str:
        runtime: Any = invocation.context.runtime
        control = getattr(runtime, "work_control", None)
        session = getattr(control, "session", None)
        if session is None:
            return await execute()
        if invocation.identity.parent_operation_id is not None:
            # Composition admission/checkpoints are introduced in P02. Never silently
            # use a top-level Provider key for an unimplemented child invocation.
            raise ValueError("composition_invocation_not_supported")
        if session.call_key(invocation.call.id) != invocation.identity.operation_id:
            raise ValueError("invocation_journal_identity_conflict")
        return str(
            await session.execute(
                invocation.call,
                execute,
                side_effecting=side_effecting,
                allow_pending=invocation.call.function.name == "send_message",
                invocation=invocation,
            )
        )
