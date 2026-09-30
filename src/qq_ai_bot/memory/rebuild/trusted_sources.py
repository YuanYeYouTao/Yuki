"""Ephemeral identity fences for existing trusted mention/reply hydration."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.identity.canonical_repository import IDENTITY_PLATFORM
from qq_ai_bot.identity.db_models import CanonicalPersonModel, IdentityBindingModel, PresenceModel
from qq_ai_bot.persistence.models import ChatEventModel
from qq_ai_bot.persistence.repository_records import EventRecord


@dataclass(frozen=True, slots=True)
class TrustedSourceFence:
    account_ids: tuple[str, ...]
    reply_event_id: int | None
    identity_tokens: tuple[Any, ...]
    reply_token: tuple[Any, ...] | None


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
    session: AsyncSession, ids: tuple[str, ...]
) -> dict[str, tuple[Any, ...]]:
    if not ids:
        return {}
    binding = IdentityBindingModel
    person = CanonicalPersonModel
    bindings = {
        str(row.external_account_id): tuple(row)
        for row in await session.execute(
            select(
                binding.external_account_id,
                binding.id,
                binding.person_id,
                binding.status,
                binding.revision,
                person.enabled,
                person.revision,
            )
            .outerjoin(person, person.id == binding.person_id)
            .where(binding.platform == IDENTITY_PLATFORM, binding.external_account_id.in_(ids))
        )
    }
    presence = PresenceModel
    presences = {
        str(row.external_account_id): tuple(row)
        for row in await session.execute(
            select(
                presence.external_account_id, presence.id, presence.enabled, presence.revision
            ).where(presence.platform == IDENTITY_PLATFORM, presence.external_account_id.in_(ids))
        )
    }
    return {account: (bindings.get(account), presences.get(account)) for account in ids}


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
    accounts: dict[int, tuple[str, ...]] = {}
    for event in events:
        refs = list(event.mentioned_user_ids)
        if not refs:
            for segment in event.segments:
                data = segment.get("data")
                if segment.get("type") == "at" and isinstance(data, dict):
                    raw = str(data.get("qq", "")).strip()
                    if raw.isdigit():
                        refs.append(raw)
        if event.reply_sender_user_id:
            refs.append(event.reply_sender_user_id)
        elif event.reply_to_event_id in replies:
            # get_reply_event uses this internal ID, never a platform-ID lookup.
            refs.append(str(replies[event.reply_to_event_id][5]))
        accounts[event.id] = tuple(sorted({str(ref).strip() for ref in refs if str(ref).strip()}))
    identities = await _identity_tokens(
        session, tuple(sorted({account for refs in accounts.values() for account in refs}))
    )
    return {
        event.id: TrustedSourceFence(
            account_ids=accounts[event.id],
            reply_event_id=event.reply_to_event_id,
            identity_tokens=tuple(identities[account] for account in accounts[event.id]),
            reply_token=replies.get(event.reply_to_event_id) if event.reply_to_event_id else None,
        )
        for event in events
    }


async def changed_trusted_sources(
    session: AsyncSession, prepared: dict[int, TrustedSourceFence]
) -> frozenset[int]:
    """Batch-check the same references under the writer, without rehydrating any body."""
    accounts = tuple(
        sorted({account for fence in prepared.values() for account in fence.account_ids})
    )
    reply_ids = tuple(
        sorted({fence.reply_event_id for fence in prepared.values() if fence.reply_event_id})
    )
    identities = await _identity_tokens(session, accounts)
    replies = await _reply_tokens(session, reply_ids)
    return frozenset(
        event_id
        for event_id, fence in prepared.items()
        if fence.identity_tokens != tuple(identities[account] for account in fence.account_ids)
        or fence.reply_token
        != (replies.get(fence.reply_event_id) if fence.reply_event_id else None)
    )
