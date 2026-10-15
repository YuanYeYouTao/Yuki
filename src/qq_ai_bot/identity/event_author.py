"""Canonical event-author projection shared by ledger writers."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from qq_ai_bot.domain.identity import AuthorKind
from qq_ai_bot.identity.canonical_repository import (
    IDENTITY_PLATFORM,
    external_id,
)
from qq_ai_bot.identity.db_models import CanonicalPersonModel, IdentityBindingModel, PresenceModel
from qq_ai_bot.identity.errors import CanonicalIdentityError
from qq_ai_bot.identity.write_settings import identity_write_settings


@dataclass(frozen=True, slots=True)
class EventAuthorProjection:
    """Canonical author triple. Origin and event_kind stay outside this type."""

    author_kind: str
    author_person_id: str | None
    author_presence_id: str | None

    def as_tuple(self) -> tuple[str, str | None, str | None]:
        return (self.author_kind, self.author_person_id, self.author_presence_id)


async def project_event_author(
    session: AsyncSession,
    *,
    sender_user_id: str,
    sender_is_bot: bool = False,
    event_kind: str = "message",
    direction: str = "inbound",
    platform: str = IDENTITY_PLATFORM,
) -> EventAuthorProjection:
    """Classify a sender from existing same-platform Presence/Binding rows.

    Presence wins over current-handle equality and over ignored/explicit bot.
    Unknown Presence is never auto-registered. Missing human Binding stays
    ``person`` with a null person id so callers can create or fail closed.
    """

    if event_kind == "external_event" or direction == "external":
        return EventAuthorProjection(AuthorKind.SYSTEM.value, None, None)
    sender_id = external_id(sender_user_id)
    return (
        await project_event_authors(
            session, sender_user_ids=(sender_id,), sender_is_bot=sender_is_bot, platform=platform
        )
    )[sender_id]


async def project_event_authors(
    session: AsyncSession,
    *,
    sender_user_ids: tuple[str, ...],
    sender_is_bot: bool = False,
    platform: str = IDENTITY_PLATFORM,
) -> dict[str, EventAuthorProjection]:
    """Project the requested accounts together using the same canonical rules."""

    accounts = tuple(dict.fromkeys(external_id(item) for item in sender_user_ids))
    if not accounts:
        return {}
    values = func.json_each(json.dumps(accounts)).table_valued("value")
    selected = select(values.c.value)
    presences = {
        account: presence_id
        for account, presence_id in (
            await session.execute(
                select(PresenceModel.external_account_id, PresenceModel.id).where(
                    PresenceModel.platform == platform,
                    PresenceModel.external_account_id.in_(selected),
                )
            )
        ).all()
    }
    bindings = {
        row.external_account_id: row
        for row in (
            await session.execute(
                select(
                    IdentityBindingModel.external_account_id,
                    IdentityBindingModel.person_id,
                    IdentityBindingModel.status,
                    CanonicalPersonModel.enabled,
                )
                .outerjoin(
                    CanonicalPersonModel, CanonicalPersonModel.id == IdentityBindingModel.person_id
                )
                .where(
                    IdentityBindingModel.platform == platform,
                    IdentityBindingModel.external_account_id.in_(selected),
                    IdentityBindingModel.status != "transferred",
                )
            )
        ).all()
    }
    settings = identity_write_settings()
    authors: dict[str, EventAuthorProjection] = {}
    for sender_id in accounts:
        presence_id = presences.get(sender_id)
        binding = bindings.get(sender_id)
        if presence_id is not None and binding is not None:
            raise CanonicalIdentityError("canonical_kind_mismatch")
        if presence_id is not None:
            author = EventAuthorProjection(AuthorKind.YUKI.value, None, presence_id)
        elif sender_is_bot or sender_id in settings.ignored_bot_users:
            author = EventAuthorProjection(AuthorKind.EXTERNAL_BOT.value, None, None)
        elif binding is not None and binding.status == "active":
            if binding.enabled is None:
                raise CanonicalIdentityError("unclassified")
            if not binding.enabled:
                raise CanonicalIdentityError("canonical_owner_disabled")
            author = EventAuthorProjection(AuthorKind.PERSON.value, binding.person_id, None)
        else:
            author = EventAuthorProjection(AuthorKind.PERSON.value, None, None)
        authors[sender_id] = author
    return authors


async def same_platform_presence_external_ids(session: AsyncSession) -> frozenset[str]:
    """Existing same-platform Presence external accounts. Never auto-registers."""

    rows = list(
        await session.scalars(
            select(PresenceModel).where(PresenceModel.platform == IDENTITY_PLATFORM)
        )
    )
    return frozenset(item.external_account_id for item in rows)


async def canonical_person_reference_ids(
    session: AsyncSession,
    user_ids: tuple[str, ...],
    *,
    speaker_user_id: str,
    authors: Mapping[str, EventAuthorProjection] | None = None,
) -> tuple[str, ...]:
    """Keep one Binding representative per Person. Drop Yuki/external/unbound."""

    candidates = tuple(
        dict.fromkeys(
            str(raw).strip()
            for raw in user_ids
            if str(raw).strip() and str(raw).strip() != speaker_user_id
        )
    )
    if authors is None:
        authors = await project_event_authors(session, sender_user_ids=candidates)
    seen_persons: set[str] = set()
    kept: list[str] = []
    for user_id in candidates:
        author = authors[user_id]
        if author.author_kind != AuthorKind.PERSON.value or author.author_person_id is None:
            continue
        if author.author_person_id in seen_persons:
            continue
        seen_persons.add(author.author_person_id)
        kept.append(user_id)
    return tuple(kept)


async def canonical_account_is_person(session: AsyncSession, user_id: str) -> bool:
    author = await project_event_author(session, sender_user_id=user_id)
    return author.author_kind == AuthorKind.PERSON.value and author.author_person_id is not None


__all__ = [
    "EventAuthorProjection",
    "canonical_account_is_person",
    "canonical_person_reference_ids",
    "project_event_author",
    "project_event_authors",
    "same_platform_presence_external_ids",
]
