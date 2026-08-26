"""Canonical event-author projection shared by ledger writers."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from qq_ai_bot.domain.identity import AuthorKind
from qq_ai_bot.identity.canonical_repository import (
    IDENTITY_PLATFORM,
    external_id,
    find_identity_binding,
    find_presence,
)
from qq_ai_bot.identity.db_models import CanonicalPersonModel, PresenceModel
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
) -> EventAuthorProjection:
    """Classify a sender from existing same-platform Presence/Binding rows.

    Presence wins over current-handle equality and over ignored/explicit bot.
    Unknown Presence is never auto-registered. Missing human Binding stays
    ``person`` with a null person id so callers can create or fail closed.
    """

    if event_kind == "external_event" or direction == "external":
        return EventAuthorProjection(AuthorKind.SYSTEM.value, None, None)
    sender_id = external_id(sender_user_id)
    presence = await find_presence(session, sender_id)
    binding = await find_identity_binding(session, sender_id)
    if presence is not None and binding is not None:
        raise CanonicalIdentityError("canonical_kind_mismatch")
    if presence is not None:
        return EventAuthorProjection(AuthorKind.YUKI.value, None, presence.id)
    settings = identity_write_settings()
    if sender_is_bot or sender_id in settings.ignored_bot_users:
        return EventAuthorProjection(AuthorKind.EXTERNAL_BOT.value, None, None)
    if binding is not None and binding.status == "active":
        person = await session.get(CanonicalPersonModel, binding.person_id)
        if person is None:
            raise CanonicalIdentityError("unclassified")
        if not person.enabled:
            raise CanonicalIdentityError("canonical_owner_disabled")
        return EventAuthorProjection(AuthorKind.PERSON.value, person.id, None)
    return EventAuthorProjection(AuthorKind.PERSON.value, None, None)


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
) -> tuple[str, ...]:
    """Keep one Binding representative per Person. Drop Yuki/external/unbound."""

    seen_persons: set[str] = set()
    kept: list[str] = []
    for raw in user_ids:
        user_id = str(raw).strip()
        if not user_id or user_id == speaker_user_id:
            continue
        author = await project_event_author(session, sender_user_id=user_id)
        if author.author_kind != AuthorKind.PERSON.value or author.author_person_id is None:
            continue
        if author.author_person_id in seen_persons:
            continue
        seen_persons.add(author.author_person_id)
        kept.append(user_id)
        if len(kept) >= 5:
            break
    return tuple(kept)


async def canonical_account_is_person(session: AsyncSession, user_id: str) -> bool:
    author = await project_event_author(session, sender_user_id=user_id)
    return author.author_kind == AuthorKind.PERSON.value and author.author_person_id is not None


__all__ = [
    "EventAuthorProjection",
    "canonical_account_is_person",
    "canonical_person_reference_ids",
    "project_event_author",
    "same_platform_presence_external_ids",
]
