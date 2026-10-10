"""Preparation waits belong to the activation, not the context assembler."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from qq_ai_bot.persistence.event_repository import ConversationReadVersion
    from qq_ai_bot.runtime.work_control import WorkControl
    from qq_ai_bot.runtime.work_journal import JournalSnapshot
    from qq_ai_bot.runtime.work_source_guard import WorkSourceGuard


class ContextPreparationMode(Enum):
    FOREGROUND = "foreground"
    DURABLE = "durable"
    FALLBACK = "fallback"


context_preparation_mode: ContextVar[ContextPreparationMode] = ContextVar(
    "context_preparation_mode", default=ContextPreparationMode.FOREGROUND
)


@dataclass(frozen=True, slots=True)
class ProtocolRecoveryPreparation:
    snapshot: JournalSnapshot
    guard: WorkSourceGuard


protocol_recovery_preparation: ContextVar[ProtocolRecoveryPreparation | None] = ContextVar(
    "protocol_recovery_preparation", default=None
)


async def select_protocol_recovery(
    control: WorkControl | None,
    contract: str | None,
) -> ProtocolRecoveryPreparation | None:
    """Choose exact private recovery before any fresh chat/material selection."""
    if control is None or control.current is None or contract is None:
        return None
    from qq_ai_bot.runtime.work_journal import WorkJournal
    from qq_ai_bot.runtime.work_repository import WorkConflict
    from qq_ai_bot.runtime.work_source_guard import WorkSourceGuard

    snapshot = await WorkJournal(control.repository).load(
        control.lease, control.current["id"], contract, source_control=control
    )
    if snapshot.reason != "resume" or snapshot.record is None:
        return None
    payload = json.loads(snapshot.record["payload_json"])
    metadata = payload.get("metadata", {})
    progress = metadata.get("progress", {})
    exact = (
        bool(control.lease.work_id)
        or snapshot.record["phase"] in {"delivery", "delivered"}
        or bool(progress.get("provider_pause_replay"))
        or (bool(progress.get("compaction_staging")) and snapshot.record["phase"] != "response")
    )
    saved_guard = metadata.get("source_guard")
    if not exact or not saved_guard:
        # Old records without the persisted actual read set retain the existing
        # conservative preparation path; fresh reads never certify an old guard.
        return None
    guard = WorkSourceGuard.restore(saved_guard)
    if not await guard.check(control):
        raise WorkConflict("work_source_changed")
    return ProtocolRecoveryPreparation(snapshot, guard)


class ContextRollupRequired(RuntimeError):
    """A bounded context requires coverage before it can be prepared."""

    def __init__(
        self,
        version: ConversationReadVersion,
        coverage: int,
        timeout_seconds: float,
        *,
        token_budget: int | None = None,
    ) -> None:
        super().__init__("context_rollup_required")
        self.version = version
        self.coverage = coverage
        self.timeout_seconds = timeout_seconds
        self.token_budget = token_budget


async def prepare_context[T](
    builder: Callable[[], Awaitable[T]],
    control: WorkControl | None,
    *,
    recovery_contract: str | None = None,
) -> T:
    """Build once, or park the original pre-history Work without holding a slot.

    The existing repository validates the original source, rejects frozen model
    history, and retains the prerequisite's original deadline. An expired/failed
    prerequisite permits only the existing extractive fallback on a second build.
    """
    owned = control is not None and control.current is not None
    mode = ContextPreparationMode.DURABLE if owned else ContextPreparationMode.FOREGROUND
    recovery = await select_protocol_recovery(control, recovery_contract)
    if control is not None:
        control.protocol_recovery_preparation = recovery
    token = context_preparation_mode.set(mode)
    recovery_token = protocol_recovery_preparation.set(recovery)
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
                token_budget=requirement.token_budget,
            ):
                from qq_ai_bot.runtime.work_control import WorkInputsPreparing

                control.ending = "waiting_external"
                await control.settle(pending_inputs=bool(await control.pending()))
                raise WorkInputsPreparing("work_context_preparing") from requirement
            context_preparation_mode.set(ContextPreparationMode.FALLBACK)
            prepared = await builder()
        if control is not None and control.current is not None:
            if json.loads(control.current["checkpoint_json"]).get("context_rollup"):
                await control.repository.finish_context_rollup(control.lease, control.current["id"])
        return prepared
    finally:
        protocol_recovery_preparation.reset(recovery_token)
        context_preparation_mode.reset(token)
