"""Preparation waits belong to the activation, not the context assembler."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import select

from yuki.capabilities.invocation import direct_operation_id
from yuki.runtime.work_schema_v1 import effects

if TYPE_CHECKING:
    from yuki.runtime.work_control import WorkControl
    from yuki.runtime.work_journal import JournalSnapshot
    from yuki.runtime.work_source_guard import WorkSourceGuard


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
    from yuki.runtime.work_journal import WorkJournal
    from yuki.runtime.work_repository import WorkConflict
    from yuki.runtime.work_source_guard import WorkSourceGuard

    snapshot = await WorkJournal(control.repository).load(
        control.lease, control.current["id"], contract, source_control=control
    )
    if snapshot.reason != "resume" or snapshot.record is None:
        return None
    payload = json.loads(snapshot.record["payload_json"])
    metadata = payload.get("metadata", {})
    progress = metadata.get("progress", {})
    exact = bool(
        progress.get("provider_pause_replay")
        or payload["transcript"].get("continuation")
        or (control.lease.work_id and progress.get("context_compaction"))
    )
    pending = payload.get("pending", [])
    if pending and not exact:
        keys = tuple(
            direct_operation_id(
                snapshot.record["chain_id"], metadata.get("sequence", 0), call["id"]
            )
            for call in pending
            if "readonly_result_key" not in call
        )
        async with control.repository.database.sessions() as reader:
            receipts = {
                row.effect_key: row
                for row in await reader.execute(
                    select(
                        effects.c.effect_key,
                        effects.c.kind,
                        effects.c.state,
                        effects.c.receipt_json,
                    ).where(
                        effects.c.work_id == control.current["id"], effects.c.effect_key.in_(keys)
                    )
                )
            }
        for key in keys:
            row = receipts.get(key)
            if row is None:
                exact = True
                break
            receipt = json.loads(row.receipt_json)
            composition = receipt.get("composition")
            invocation = receipt.get("invocation")
            if (
                row.kind == "code_composition"
                and row.state in {"prepared", "unknown"}
                and isinstance(composition, dict)
                and composition.get("version") == 1
            ) or (
                row.state == "prepared"
                and isinstance(invocation, dict)
                and invocation.get("version") == 1
                and invocation.get("dispatch_started") is False
            ):
                exact = True
                break
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
