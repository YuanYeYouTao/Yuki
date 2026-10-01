"""Preparation waits belong to the activation, not the context assembler."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from qq_ai_bot.persistence.event_repository import ConversationReadVersion
    from qq_ai_bot.runtime.work_control import WorkControl


class ContextPreparationMode(Enum):
    FOREGROUND = "foreground"
    DURABLE = "durable"
    FALLBACK = "fallback"


context_preparation_mode: ContextVar[ContextPreparationMode] = ContextVar(
    "context_preparation_mode", default=ContextPreparationMode.FOREGROUND
)


class ContextRollupRequired(RuntimeError):
    """A bounded context requires coverage before it can be prepared."""

    def __init__(
        self, version: ConversationReadVersion, coverage: int, timeout_seconds: float
    ) -> None:
        super().__init__("context_rollup_required")
        self.version = version
        self.coverage = coverage
        self.timeout_seconds = timeout_seconds


async def prepare_context[T](builder: Callable[[], Awaitable[T]], control: WorkControl | None) -> T:
    """Build once, or park the original pre-history Work without holding a slot.

    The existing repository validates the original source, rejects frozen model
    history, and retains the prerequisite's original deadline. An expired/failed
    prerequisite permits only the existing extractive fallback on a second build.
    """
    owned = control is not None and control.current is not None
    mode = ContextPreparationMode.DURABLE if owned else ContextPreparationMode.FOREGROUND
    token = context_preparation_mode.set(mode)
    try:
        try:
            prepared = await builder()
        except ContextRollupRequired as requirement:
            # Only an owned activation asks the assembler for durable preparation.
            if control is None or control.current is None:
                raise
            if await control.repository.defer_context_rollup(
                control.lease,
                control.current["id"],
                requirement.version,
                requirement.coverage,
                requirement.timeout_seconds,
            ):
                from qq_ai_bot.runtime.work_control import WorkInputsPreparing

                control.ending = "waiting_external"
                await control.meter_active_time()
                await control.settle(delivered=False, pending_inputs=bool(await control.pending()))
                raise WorkInputsPreparing("work_context_preparing") from requirement
            context_preparation_mode.set(ContextPreparationMode.FALLBACK)
            prepared = await builder()
        if control is not None and control.current is not None:
            if json.loads(control.current["checkpoint_json"]).get("context_rollup"):
                await control.repository.finish_context_rollup(control.lease, control.current["id"])
        return prepared
    finally:
        context_preparation_mode.reset(token)
