"""Preparation waits belong to the activation, not the context assembler."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from qq_ai_bot.runtime.work_control import WorkControl
    from qq_ai_bot.runtime.work_journal import JournalSnapshot
    from qq_ai_bot.runtime.work_source_guard import WorkSourceGuard


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


async def prepare_context[T](
    builder: Callable[[], Awaitable[T]],
    control: WorkControl | None,
    *,
    recovery_contract: str | None = None,
) -> T:
    """Select original protocol recovery before fresh public-history preparation."""
    recovery = await select_protocol_recovery(control, recovery_contract)
    if control is not None:
        control.protocol_recovery_preparation = recovery
    recovery_token = protocol_recovery_preparation.set(recovery)
    try:
        return await builder()
    finally:
        protocol_recovery_preparation.reset(recovery_token)
