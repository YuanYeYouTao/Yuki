"""Prepare exact alias selection rewrites before atomic privacy deletion."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import String, and_, delete, exists, func, or_, select, update
from sqlalchemy import cast as sql_cast
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from yuki.conversation.canonical_db_models import CanonicalConversationModel
from yuki.identity.db_models import CanonicalPersonModel, IdentityBindingModel, PresenceModel
from yuki.identity.ownership_interval import BindingOwnership
from yuki.identity.person_identity import current_person_ids, person_family_ids
from yuki.memory.rebuild.models import MemoryRebuildSelection
from yuki.persistence.models import (
    ChatEventModel,
    MemoryRebuildProposalModel,
    MemoryRebuildRunModel,
)


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
    if person_id is not None:
        person_ids = await person_family_ids(session, person_id)
    else:
        originals = tuple(
            await session.scalars(
                select(IdentityBindingModel.person_id).where(
                    IdentityBindingModel.platform == "qq",
                    IdentityBindingModel.external_account_id.in_(aliases),
                    IdentityBindingModel.status != "ended",
                )
            )
        )
        roots = tuple(set((await current_person_ids(session, originals)).values()))
        person_ids = tuple(
            await session.scalars(
                select(CanonicalPersonModel.id).where(
                    or_(
                        CanonicalPersonModel.id.in_(roots),
                        CanonicalPersonModel.merged_into_person_id.in_(roots),
                    )
                )
            )
        )
    conversations = tuple(
        await session.scalars(
            select(CanonicalConversationModel.id).where(
                CanonicalConversationModel.kind == "private",
                CanonicalConversationModel.person_id.in_(person_ids),
            )
        )
    )
    erased_events = select(ChatEventModel.id).where(
        or_(
            ChatEventModel.author_person_id.in_(person_ids),
            ChatEventModel.canonical_conversation_id.in_(conversations),
        )
    )
    run = MemoryRebuildRunModel
    people = func.json_each(run.selection_json, "$.person_ids").table_valued("value")
    scopes = func.json_each(run.selection_json, "$.conversation_ids").table_valued("value")
    events = func.json_each(run.selection_json, "$.event_ids").table_valued("value")
    original = func.json_tree(run.original_selection_json).table_valued("atom")
    identifiers = tuple({*aliases, *person_ids, *(item.account for item in ownership)})
    rows = (
        await session.execute(
            select(
                run.id,
                run.selection_hash,
                run.updated_at,
                run.status,
                run.selection_json,
                exists(
                    select(original.c.atom).where(
                        sql_cast(original.c.atom, String).in_(identifiers)
                    )
                ).label("erase_original"),
            ).where(
                or_(
                    exists(select(people.c.value).where(people.c.value.in_(person_ids))),
                    exists(select(scopes.c.value).where(scopes.c.value.in_(conversations))),
                    exists(select(events.c.value).where(events.c.value.in_(erased_events))),
                    exists(
                        select(original.c.atom).where(
                            sql_cast(original.c.atom, String).in_(identifiers)
                        )
                    ),
                )
            )
        )
    ).all()
    selections = [
        (row, MemoryRebuildSelection.model_validate_json(row.selection_json)) for row in rows
    ]
    selected_events = {
        event_id for _, selection in selections for event_id in selection.event_ids or ()
    }
    deleted_ids = (
        set(await session.scalars(erased_events.where(ChatEventModel.id.in_(selected_events))))
        if selected_events
        else set()
    )
    changes = []
    for row, selection in selections:
        remaining = tuple(value for value in selection.person_ids if value not in person_ids)
        remaining_scopes = tuple(
            value for value in selection.conversation_ids if value not in conversations
        )
        remaining_events = selection.event_ids
        if remaining_events:
            remaining_events = tuple(
                value for value in remaining_events if value not in deleted_ids
            )
        # Removing the last member of an intersection must never widen it.
        if (selection.person_ids and not remaining) or (
            selection.conversation_ids and not remaining_scopes
        ):
            remaining_events = ()
        sanitized = selection.model_copy(
            update={
                "person_ids": remaining,
                "conversation_ids": remaining_scopes,
                "event_ids": remaining_events,
            }
        )
        encoded = json.dumps(
            sanitized.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        if sanitized == selection:
            encoded = row.selection_json
        changes.append(
            (
                row.id,
                row.selection_hash,
                row.updated_at,
                row.status,
                encoded,
                row.selection_hash
                if encoded == row.selection_json
                else hashlib.sha256(encoded.encode()).hexdigest(),
                bool(row.erase_original),
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
    for (
        run_id,
        old_hash,
        old_time,
        status,
        encoded,
        fingerprint,
        erase_original,
    ) in prepared.changes:
        values: dict[str, Any] = dict(
            selection_json=encoded, selection_hash=fingerprint, updated_at=now
        )
        if erase_original:
            values["original_selection_json"] = None
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
