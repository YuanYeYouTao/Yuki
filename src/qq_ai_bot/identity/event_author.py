"""Complete-v2 event author projection shared by scoped and canonical writers."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from qq_ai_bot.domain.identity import AuthorKind
from qq_ai_bot.identity.db_models import CanonicalPersonModel, PresenceModel
from qq_ai_bot.identity.dual_write import _binding_for, _external_id, _presence_for
from qq_ai_bot.identity.errors import IdentityDualWriteError
from qq_ai_bot.identity.inventory import IDENTITY_PLATFORM
from qq_ai_bot.identity.write_settings import identity_write_settings


@dataclass(frozen=True, slots=True)
class CompleteV2EventAuthor:
    """Canonical author triple. Origin and event_kind stay outside this type."""

    author_kind: str
    author_person_id: str | None
    author_presence_id: str | None

    def as_tuple(self) -> tuple[str, str | None, str | None]:
        return (self.author_kind, self.author_person_id, self.author_presence_id)


async def project_complete_v2_event_author(
    session: AsyncSession,
    *,
    sender_user_id: str,
    sender_is_bot: bool = False,
    event_kind: str = "message",
    direction: str = "inbound",
) -> CompleteV2EventAuthor:
    """Classify a sender from existing same-platform Presence/Binding rows.

    Presence wins over current-handle equality and over ignored/explicit bot.
    Unknown Presence is never auto-registered. Missing human Binding stays
    ``person`` with a null person id so callers can create or fail closed.
    """

    if event_kind == "external_event" or direction == "external":
        return CompleteV2EventAuthor(AuthorKind.SYSTEM.value, None, None)
    sender_id = _external_id(sender_user_id)
    presence = await _presence_for(session, sender_id)
    binding = await _binding_for(session, sender_id)
    if presence is not None and binding is not None:
        raise IdentityDualWriteError("canonical_kind_mismatch")
    if presence is not None:
        return CompleteV2EventAuthor(AuthorKind.YUKI.value, None, presence.id)
    settings = identity_write_settings()
    if sender_is_bot or sender_id in settings.ignored_bot_users:
        return CompleteV2EventAuthor(AuthorKind.EXTERNAL_BOT.value, None, None)
    if binding is not None and binding.status == "active":
        person = await session.get(CanonicalPersonModel, binding.person_id)
        if person is None:
            raise IdentityDualWriteError("unclassified")
        if not person.enabled:
            raise IdentityDualWriteError("canonical_owner_disabled")
        return CompleteV2EventAuthor(AuthorKind.PERSON.value, person.id, None)
    return CompleteV2EventAuthor(AuthorKind.PERSON.value, None, None)


async def same_platform_presence_external_ids(session: AsyncSession) -> frozenset[str]:
    """Existing same-platform Presence external accounts. Never auto-registers."""

    rows = list(
        await session.scalars(
            select(PresenceModel).where(PresenceModel.platform == IDENTITY_PLATFORM)
        )
    )
    return frozenset(item.external_account_id for item in rows)


async def complete_v2_person_reference_ids(
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
        author = await project_complete_v2_event_author(session, sender_user_id=user_id)
        if author.author_kind != AuthorKind.PERSON.value or author.author_person_id is None:
            continue
        if author.author_person_id in seen_persons:
            continue
        seen_persons.add(author.author_person_id)
        kept.append(user_id)
        if len(kept) >= 5:
            break
    return tuple(kept)


async def complete_v2_account_is_person(session: AsyncSession, user_id: str) -> bool:
    author = await project_complete_v2_event_author(session, sender_user_id=user_id)
    return author.author_kind == AuthorKind.PERSON.value and author.author_person_id is not None
