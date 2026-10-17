"""Ephemeral identity fences for existing trusted mention/reply hydration."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from yuki.domain.conversations import ScopeType
from yuki.identity.db_models import IdentityBindingModel, PresenceModel
from yuki.identity.ownership_interval import BindingOwnership
from yuki.persistence.models import ChatEventModel
from yuki.persistence.repository_records import EventRecord


@dataclass(frozen=True, slots=True)
class TrustedSourceFence:
    account_ids: tuple[str, ...]
    reply_event_id: int | None
    identity_tokens: tuple[Any, ...]
    reply_token: tuple[Any, ...] | None
    platform: str | None = None
    observed_at: datetime | None = None


async def _reply_tokens(session: AsyncSession, ids: tuple[int, ...]) -> dict[int, tuple[Any, ...]]:
    if not ids:
        return {}
    event = ChatEventModel
    return {
        int(row.id): tuple(row)
        for row in await session.execute(
            select(
                event.id,
                event.canonical_conversation_id,
                event.event_kind,
                event.scope_type,
                event.group_id,
                event.sender_user_id,
                event.author_kind,
                event.author_person_id,
                event.author_presence_id,
                event.canonical_event_id,
                event.suppression_status,
            ).where(event.id.in_(ids))
        )
    }


async def _identity_tokens(
    session: AsyncSession, ids: tuple[tuple[str | None, str, datetime | None], ...]
) -> dict[tuple[str | None, str, datetime | None], tuple[Any, ...]]:
    if not ids:
        return {}
    bindings: dict[tuple[str | None, str], list[tuple[BindingOwnership, str]]] = defaultdict(list)
    for binding in await session.scalars(
        select(IdentityBindingModel).where(
            tuple_(IdentityBindingModel.platform, IdentityBindingModel.external_account_id).in_(
                tuple({(platform, account) for platform, account, _at in ids})
            )
        )
    ):
        bindings[(binding.platform, binding.external_account_id)].append(
            (BindingOwnership.of(binding), binding.person_id)
        )
    return {
        key: tuple(
            sorted(
                {
                    owner
                    for interval, owner in bindings.get((key[0], key[1]), ())
                    if key[2] is not None and interval.contains(key[2])
                }
            )
        )
        for key in ids
    }


async def prepare_trusted_sources(
    session: AsyncSession, events: tuple[EventRecord, ...]
) -> dict[int, TrustedSourceFence]:
    """Freeze only candidate transport references; ownership is still resolved by the ledger."""
    events = tuple(
        event for event in events if event.scope_type is ScopeType.GROUP and event.group_id
    )
    reply_ids = tuple(
        sorted({event.reply_to_event_id for event in events if event.reply_to_event_id})
    )
    replies = await _reply_tokens(session, reply_ids)
    platforms = {
        presence_id: platform
        for presence_id, platform in await session.execute(
            select(PresenceModel.id, PresenceModel.platform).where(
                PresenceModel.id.in_(
                    tuple(
                        event.ingress_presence_id for event in events if event.ingress_presence_id
                    )
                )
            )
        )
    }
    observed = dict(
        (
            await session.execute(
                select(ChatEventModel.id, ChatEventModel.observed_at).where(
                    ChatEventModel.id.in_(tuple(event.id for event in events))
                )
            )
        )
        .tuples()
        .all()
    )
    accounts: dict[int, tuple[str, ...]] = {}
    for event in events:
        recorded = bool(event.mentioned_person_ids) or any(
            part.get("type") == "yuki_context"
            and isinstance(data := part.get("data"), dict)
            and "mentioned_person_ids" in data
            for part in event.segments
        )
        refs = [] if recorded else list(event.mentioned_user_ids)
        if not recorded and not refs and platforms.get(event.ingress_presence_id) == "qq":
            for segment in event.segments:
                data = segment.get("data")
                if segment.get("type") == "at" and isinstance(data, dict):
                    raw = str(data.get("qq", "")).strip()
                    if raw.isdigit():
                        refs.append(raw)
        accounts[event.id] = tuple(sorted({str(ref).strip() for ref in refs if str(ref).strip()}))
    identities = await _identity_tokens(
        session,
        tuple(
            {
                (platforms.get(event.ingress_presence_id), account, observed.get(event.id))
                for event in events
                for account in accounts[event.id]
            }
        ),
    )
    return {
        event.id: TrustedSourceFence(
            account_ids=accounts[event.id],
            reply_event_id=event.reply_to_event_id,
            identity_tokens=tuple(
                identities[
                    (platforms.get(event.ingress_presence_id), account, observed.get(event.id))
                ]
                for account in accounts[event.id]
            ),
            reply_token=replies.get(event.reply_to_event_id) if event.reply_to_event_id else None,
            platform=platforms.get(event.ingress_presence_id),
            observed_at=observed.get(event.id),
        )
        for event in events
    }


async def changed_trusted_sources(
    session: AsyncSession, prepared: dict[int, TrustedSourceFence]
) -> frozenset[int]:
    """Batch-check the same references under the writer, without rehydrating any body."""
    accounts = tuple(
        {
            (fence.platform, account, fence.observed_at)
            for fence in prepared.values()
            for account in fence.account_ids
        }
    )
    reply_ids = tuple(
        sorted({fence.reply_event_id for fence in prepared.values() if fence.reply_event_id})
    )
    identities = await _identity_tokens(session, accounts)
    replies = await _reply_tokens(session, reply_ids)
    return frozenset(
        event_id
        for event_id, fence in prepared.items()
        if fence.identity_tokens
        != tuple(
            identities[(fence.platform, account, fence.observed_at)]
            for account in fence.account_ids
        )
        or fence.reply_token
        != (replies.get(fence.reply_event_id) if fence.reply_event_id else None)
    )
