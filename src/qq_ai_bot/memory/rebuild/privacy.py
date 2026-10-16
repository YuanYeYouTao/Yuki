"""Prepare exact alias selection rewrites before atomic privacy deletion."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import and_, delete, exists, func, or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from qq_ai_bot.identity.db_models import IdentityBindingModel, PresenceModel
from qq_ai_bot.identity.ownership_interval import BindingOwnership
from qq_ai_bot.identity.person_identity import person_family_ids
from qq_ai_bot.memory.rebuild.models import MemoryRebuildSelection
from qq_ai_bot.persistence.models import (
    ChatEventModel,
    MemoryRebuildProposalModel,
    MemoryRebuildRunModel,
)

_TERMINAL = {"completed", "cancelled", "failed"}


@dataclass(frozen=True, slots=True)
class PreparedRebuildForget:
    aliases: tuple[str, ...]
    # Original identity and the fully serialized replacement; no writer JSON parsing.
    changes: tuple[tuple[int, str, datetime, str, str, str, bool], ...]
    ownership: tuple[BindingOwnership, ...] = ()
    person_ids: tuple[str, ...] = ()


async def prepare_forget(
    session: AsyncSession,
    aliases: tuple[str, ...],
    ownership: tuple[BindingOwnership, ...] = (),
    *,
    person_id: str | None = None,
) -> PreparedRebuildForget:
    aliases = tuple(sorted(set(aliases)))
    person_ids = (
        await person_family_ids(session, person_id)
        if person_id is not None
        else tuple(
            await session.scalars(
                select(IdentityBindingModel.person_id)
                .where(
                    IdentityBindingModel.platform == "qq",
                    IdentityBindingModel.external_account_id.in_(aliases),
                    IdentityBindingModel.status != "transferred",
                )
                .distinct()
            )
        )
    )
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
    return PreparedRebuildForget(aliases, tuple(changes), ownership, person_ids)


async def apply_forget(session: AsyncSession, prepared: PreparedRebuildForget) -> int:
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
    proposal = MemoryRebuildProposalModel
    # Original canonical targets are authoritative, including third-party claims.
    conditions: list[ColumnElement[bool]] = [
        proposal.canonical_subject_person_id.in_(prepared.person_ids)
    ]
    source_platform = func.coalesce(
        select(PresenceModel.platform)
        .where(PresenceModel.id == ChatEventModel.ingress_presence_id)
        .scalar_subquery(),
        "qq",  # Legacy events without an ingress Presence predate other platforms.
    )
    conditions.append(
        and_(
            proposal.canonical_subject_person_id.is_(None),
            proposal.subject_user_id.in_(prepared.aliases),
            exists(
                select(ChatEventModel.id).where(
                    ChatEventModel.id == proposal.event_id, source_platform == "qq"
                )
            ),
        )
    )
    for interval in prepared.ownership:
        source = [
            ChatEventModel.id == proposal.event_id,
            source_platform == interval.platform,
        ]
        if interval.starts_at is not None:
            source.append(ChatEventModel.observed_at >= interval.starts_at)
        if interval.ends_at is not None:
            source.append(ChatEventModel.observed_at < interval.ends_at)
        conditions.append(
            and_(
                proposal.canonical_subject_person_id.is_(None),
                proposal.subject_user_id == interval.account,
                exists(select(ChatEventModel.id).where(*source)),
            )
        )
    deleted = await session.execute(delete(proposal).where(or_(*conditions)))
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
