"""Prepare exact alias selection rewrites before atomic privacy deletion."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import delete, exists, func, or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from qq_ai_bot.memory.rebuild.models import MemoryRebuildSelection
from qq_ai_bot.persistence.models import MemoryRebuildProposalModel, MemoryRebuildRunModel

_TERMINAL = {"completed", "cancelled", "failed"}


@dataclass(frozen=True, slots=True)
class PreparedRebuildForget:
    aliases: tuple[str, ...]
    catalogue_token: tuple[Any, ...]
    # Original identity and the fully serialized replacement; no writer JSON parsing.
    changes: tuple[tuple[int, str, datetime, str, str, str, bool], ...]


async def _catalogue_token(session: AsyncSession) -> tuple[Any, ...]:
    run = MemoryRebuildRunModel
    return tuple(
        tuple(row)
        for row in await session.execute(
            select(run.id, run.selection_hash, run.status, run.updated_at).order_by(run.id)
        )
    )


async def prepare_forget(session: AsyncSession, aliases: tuple[str, ...]) -> PreparedRebuildForget:
    aliases = tuple(sorted(set(aliases)))
    catalogue = await _catalogue_token(session)
    run = MemoryRebuildRunModel
    senders = func.json_each(run.selection_json, "$.sender_user_ids").table_valued("value")
    bots = func.json_each(run.selection_json, "$.bot_user_ids").table_valued("value")
    rows = await session.execute(
        select(run.id, run.selection_hash, run.updated_at, run.status, run.selection_json).where(
            or_(
                exists(select(senders.c.value).where(senders.c.value.in_(aliases))),
                exists(select(bots.c.value).where(bots.c.value.in_(aliases))),
            )
        )
    )
    changes = []
    for row in rows:
        selection = MemoryRebuildSelection.model_validate_json(row.selection_json)
        remaining = tuple(value for value in selection.sender_user_ids if value not in aliases)
        remaining_bots = tuple(value for value in selection.bot_user_ids if value not in aliases)
        bounded = bool(
            selection.all_events
            or remaining
            or remaining_bots
            or selection.scope_types
            or selection.group_ids
            or selection.after
            or selection.before
            or selection.minimum_event_id
            or selection.maximum_event_id
        )
        sanitized = selection.model_copy(
            update={
                "sender_user_ids": remaining if bounded else ("[deleted-user]",),
                "bot_user_ids": remaining_bots,
            }
        )
        encoded = json.dumps(
            sanitized.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        changes.append(
            (
                row.id,
                row.selection_hash,
                row.updated_at,
                row.status,
                encoded,
                hashlib.sha256(encoded.encode()).hexdigest(),
                not bounded,
            )
        )
    if await _catalogue_token(session) != catalogue:
        raise ValueError("memory_rebuild_privacy_preparation_changed")
    return PreparedRebuildForget(aliases, catalogue, tuple(changes))


async def apply_forget(session: AsyncSession, prepared: PreparedRebuildForget) -> int:
    if await _catalogue_token(session) != prepared.catalogue_token:
        raise ValueError("memory_rebuild_privacy_preparation_changed")
    run = MemoryRebuildRunModel
    rows = {
        row.id: (row.selection_hash, row.updated_at, row.status)
        for row in await session.execute(
            select(run.id, run.selection_hash, run.updated_at, run.status).where(
                run.id.in_(tuple(change[0] for change in prepared.changes))
            )
        )
    }
    if any(rows.get(change[0]) != change[1:4] for change in prepared.changes):
        raise ValueError("memory_rebuild_privacy_preparation_changed")
    deleted = await session.execute(
        delete(MemoryRebuildProposalModel).where(
            MemoryRebuildProposalModel.subject_user_id.in_(prepared.aliases)
        )
    )
    changed = int(cast(CursorResult[Any], deleted).rowcount or 0)
    now = datetime.now(UTC)
    for run_id, old_hash, old_time, status, encoded, fingerprint, cancel in prepared.changes:
        values: dict[str, Any] = dict(
            selection_json=encoded, selection_hash=fingerprint, updated_at=now
        )
        if cancel and status not in _TERMINAL:
            values.update(status="cancelled", cancelled_at=now, error_category="privacy_deletion")
        result = await session.execute(
            update(run)
            .where(
                run.id == run_id,
                run.selection_hash == old_hash,
                run.updated_at == old_time,
                run.status == status,
            )
            .values(**values)
        )
        if cast(CursorResult[Any], result).rowcount != 1:
            raise ValueError("memory_rebuild_privacy_preparation_changed")
        changed += 1
    return changed
