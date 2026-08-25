"""Canonical Person, Space, Binding, and Presence persistence helpers.

The 3.8 runtime has one identity model.  This module deliberately has no
knowledge of the retired ``people``, ``groups``, or ``conversation_scopes``
carriers and performs no identity-epoch branching.  The process startup gate
is responsible for refusing a database that is not ready for this model.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Final, Literal, cast
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from qq_ai_bot.conversation.canonical_db_models import (
    CanonicalConversationModel,
    ConversationLegacyAliasModel,
    PersonActiveRouteModel,
)
from qq_ai_bot.conversation.hydrate import delete_canonical_rollup_projections
from qq_ai_bot.identity.db_models import (
    CanonicalPersonModel,
    CanonicalSpaceModel,
    IdentityBindingModel,
    PresenceModel,
    SpaceBindingModel,
)
from qq_ai_bot.identity.errors import CanonicalIdentityError
from qq_ai_bot.persistence.models import ChatEventModel

IDENTITY_PLATFORM: Final[str] = "qq"
AccountRole = Literal["human", "yuki_self", "external_bot", "private_peer"]
Failpoint = Callable[[str], None]

_failpoint: Failpoint | None = None


def set_identity_failpoint(failpoint: Failpoint | None) -> None:
    """Install deterministic failure injection used by transaction tests."""

    global _failpoint
    _failpoint = failpoint


def trip(name: str) -> None:
    if _failpoint is not None:
        _failpoint(name)


def new_identity_id() -> str:
    return str(uuid4())


def external_id(raw: str) -> str:
    normalized = optional_external_id(raw)
    if normalized is None:
        raise CanonicalIdentityError("unclassified")
    return normalized


def optional_external_id(raw: object | None) -> str | None:
    if raw is None:
        return None
    normalized = str(raw).strip()
    if not normalized or len(normalized) > 255:
        return None
    return normalized


async def find_identity_binding(
    session: AsyncSession,
    external_account_id: str,
    *,
    platform: str = IDENTITY_PLATFORM,
) -> IdentityBindingModel | None:
    return cast(
        IdentityBindingModel | None,
        await session.scalar(
            select(IdentityBindingModel).where(
                IdentityBindingModel.platform == platform,
                IdentityBindingModel.external_account_id == external_account_id,
            )
        ),
    )


async def find_presence(
    session: AsyncSession,
    external_account_id: str,
    *,
    platform: str = IDENTITY_PLATFORM,
) -> PresenceModel | None:
    return cast(
        PresenceModel | None,
        await session.scalar(
            select(PresenceModel).where(
                PresenceModel.platform == platform,
                PresenceModel.external_account_id == external_account_id,
            )
        ),
    )


async def find_space_binding(
    session: AsyncSession,
    external_space_id: str,
    *,
    platform: str = IDENTITY_PLATFORM,
) -> SpaceBindingModel | None:
    return cast(
        SpaceBindingModel | None,
        await session.scalar(
            select(SpaceBindingModel).where(
                SpaceBindingModel.platform == platform,
                SpaceBindingModel.external_space_id == external_space_id,
            )
        ),
    )


async def create_person_binding(
    session: AsyncSession,
    *,
    external_account_id: str,
    display_name: str,
    now: datetime,
    platform: str = IDENTITY_PLATFORM,
) -> IdentityBindingModel:
    """Create one Person and Binding, re-reading after a uniqueness race."""

    try:
        async with session.begin_nested():
            person = CanonicalPersonModel(
                id=new_identity_id(),
                enabled=True,
                revision=1,
                created_at=now,
                updated_at=now,
            )
            binding = IdentityBindingModel(
                id=new_identity_id(),
                person_id=person.id,
                platform=platform,
                external_account_id=external_account_id,
                display_name=display_name[:128],
                status="active",
                revision=1,
                created_at=now,
                updated_at=now,
            )
            session.add_all((person, binding))
            await session.flush()
            return binding
    except IntegrityError:
        existing = await find_identity_binding(
            session,
            external_account_id,
            platform=platform,
        )
        if existing is None:
            raise CanonicalIdentityError("canonical_kind_mismatch") from None
        return existing


async def create_presence(
    session: AsyncSession,
    *,
    external_account_id: str,
    now: datetime,
    platform: str = IDENTITY_PLATFORM,
) -> PresenceModel:
    try:
        async with session.begin_nested():
            presence = PresenceModel(
                id=new_identity_id(),
                platform=platform,
                external_account_id=external_account_id,
                enabled=True,
                ingest_eligible=True,
                revision=1,
                created_at=now,
                updated_at=now,
            )
            session.add(presence)
            await session.flush()
            return presence
    except IntegrityError:
        existing = await find_presence(session, external_account_id, platform=platform)
        if existing is None:
            raise CanonicalIdentityError("canonical_kind_mismatch") from None
        return existing


async def create_space_binding(
    session: AsyncSession,
    *,
    external_space_id: str,
    name: str,
    enabled: bool,
    autonomous_enabled: bool,
    require_mention: bool,
    now: datetime,
    platform: str = IDENTITY_PLATFORM,
) -> SpaceBindingModel:
    try:
        async with session.begin_nested():
            space = CanonicalSpaceModel(
                id=new_identity_id(),
                name=name[:128],
                enabled=enabled,
                autonomous_enabled=autonomous_enabled,
                require_mention=require_mention,
                revision=1,
                created_at=now,
                updated_at=now,
            )
            binding = SpaceBindingModel(
                id=new_identity_id(),
                space_id=space.id,
                platform=platform,
                external_space_id=external_space_id,
                display_name=name[:128],
                status="active",
                revision=1,
                created_at=now,
                updated_at=now,
            )
            session.add_all((space, binding))
            await session.flush()
            return binding
    except IntegrityError:
        existing = await find_space_binding(
            session,
            external_space_id,
            platform=platform,
        )
        if existing is None:
            raise CanonicalIdentityError("canonical_kind_mismatch") from None
        return existing


async def ensure_person(
    session: AsyncSession,
    user_id: str,
    *,
    display_name: str = "",
    now: datetime | None = None,
) -> str:
    account_id = external_id(user_id)
    binding = await find_identity_binding(session, account_id)
    presence = await find_presence(session, account_id)
    if binding is not None and presence is not None:
        raise CanonicalIdentityError("canonical_kind_mismatch")
    if presence is not None:
        raise CanonicalIdentityError("canonical_kind_mismatch")
    if binding is None:
        binding = await create_person_binding(
            session,
            external_account_id=account_id,
            display_name=display_name,
            now=now or datetime.now(UTC),
        )
        trip("after_identity_foundation")
    return binding.person_id


async def ensure_presence(
    session: AsyncSession,
    bot_user_id: str,
    *,
    now: datetime | None = None,
) -> str:
    account_id = external_id(bot_user_id)
    presence = await find_presence(session, account_id)
    binding = await find_identity_binding(session, account_id)
    if presence is not None and binding is not None:
        raise CanonicalIdentityError("canonical_kind_mismatch")
    if binding is not None:
        raise CanonicalIdentityError("canonical_kind_mismatch")
    if presence is None:
        presence = await create_presence(
            session,
            external_account_id=account_id,
            now=now or datetime.now(UTC),
        )
        trip("after_identity_foundation")
    return presence.id


async def ensure_space(
    session: AsyncSession,
    group_id: str,
    *,
    name: str = "",
    enabled: bool = True,
    autonomous_enabled: bool = True,
    require_mention: bool = True,
    now: datetime | None = None,
) -> str:
    space_external_id = external_id(group_id)
    binding = await find_space_binding(session, space_external_id)
    if binding is None:
        binding = await create_space_binding(
            session,
            external_space_id=space_external_id,
            name=name,
            enabled=enabled,
            autonomous_enabled=autonomous_enabled,
            require_mention=require_mention,
            now=now or datetime.now(UTC),
        )
        trip("after_identity_foundation")
    return binding.space_id


async def set_person_enabled_for_account(
    session: AsyncSession,
    user_id: str,
    enabled: bool,
) -> None:
    binding = await find_identity_binding(session, external_id(user_id))
    if binding is None:
        return
    person = await session.get(CanonicalPersonModel, binding.person_id)
    if person is None:
        raise CanonicalIdentityError("unclassified")
    person.enabled = enabled
    person.updated_at = datetime.now(UTC)
    person.revision = int(person.revision) + 1


async def set_space_flags_for_external(
    session: AsyncSession,
    group_id: str,
    *,
    enabled: bool | None = None,
    autonomous_enabled: bool | None = None,
    require_mention: bool | None = None,
) -> None:
    binding = await find_space_binding(session, external_id(group_id))
    if binding is None:
        return
    space = await session.get(CanonicalSpaceModel, binding.space_id)
    if space is None:
        raise CanonicalIdentityError("unclassified")
    if enabled is not None:
        space.enabled = enabled
    if autonomous_enabled is not None:
        space.autonomous_enabled = autonomous_enabled
    if require_mention is not None:
        space.require_mention = require_mention
    space.updated_at = datetime.now(UTC)
    space.revision = int(space.revision) + 1


def assert_same_shadow(current: str | None, proven: str | None) -> None:
    if proven is not None and current is not None and current != proven:
        raise CanonicalIdentityError("canonical_owner_mismatch")


async def apply_event_identity(
    session: AsyncSession,
    event: ChatEventModel,
    *,
    sender_is_bot: bool,
) -> None:
    """Fill canonical event author and ingress fields without legacy lookup."""

    from qq_ai_bot.domain.identity import AuthorKind
    from qq_ai_bot.identity.event_author import project_event_author

    trip("before_event_shadow")
    ingress = await find_presence(session, external_id(event.bot_user_id))
    event.canonical_event_id = None
    event.canonical_conversation_id = None
    event.utterance_fingerprint = None
    event.suppression_status = None
    event.ingress_provider = None
    event.ingress_gateway_instance_id = None
    event.ingress_presence_id = None if ingress is None else ingress.id
    author = await project_event_author(
        session,
        sender_user_id=event.sender_user_id,
        sender_is_bot=sender_is_bot,
        event_kind=event.event_kind,
        direction=event.direction,
    )
    if author.author_kind == AuthorKind.PERSON.value and author.author_person_id is None:
        raise CanonicalIdentityError("unclassified")
    event.author_kind = author.author_kind
    event.author_person_id = author.author_person_id
    event.author_presence_id = author.author_presence_id


async def forget_person_for_external_account(session: AsyncSession, user_id: str) -> None:
    """Delete one canonical Person aggregate selected through a Binding."""

    trip("before_forget_canonical")
    binding = await find_identity_binding(session, external_id(user_id))
    if binding is None:
        return
    person_id = binding.person_id
    bindings = list(
        await session.scalars(
            select(IdentityBindingModel).where(IdentityBindingModel.person_id == person_id)
        )
    )
    await session.execute(
        update(ChatEventModel)
        .where(ChatEventModel.author_person_id == person_id)
        .values(author_kind=None, author_person_id=None)
    )
    session.expire_all()
    route = await session.get(PersonActiveRouteModel, person_id)
    if route is not None:
        await session.delete(route)
    for item in bindings:
        await session.delete(item)
    conversation = await session.scalar(
        select(CanonicalConversationModel).where(
            CanonicalConversationModel.kind == "private",
            CanonicalConversationModel.person_id == person_id,
        )
    )
    if conversation is not None:
        await session.execute(
            update(ChatEventModel)
            .where(ChatEventModel.canonical_conversation_id == conversation.id)
            .values(canonical_conversation_id=None, canonical_event_id=None)
        )
        await delete_canonical_rollup_projections(session, conversation.id)
        aliases = list(
            await session.scalars(
                select(ConversationLegacyAliasModel).where(
                    ConversationLegacyAliasModel.conversation_id == conversation.id
                )
            )
        )
        for alias in aliases:
            await session.delete(alias)
        await session.delete(conversation)
    person = await session.get(CanonicalPersonModel, person_id)
    if person is not None:
        await session.delete(person)
    await session.flush()


__all__ = [
    "AccountRole",
    "apply_event_identity",
    "assert_same_shadow",
    "create_person_binding",
    "create_presence",
    "create_space_binding",
    "ensure_person",
    "ensure_presence",
    "ensure_space",
    "external_id",
    "find_identity_binding",
    "find_presence",
    "find_space_binding",
    "forget_person_for_external_account",
    "new_identity_id",
    "optional_external_id",
    "set_identity_failpoint",
    "set_person_enabled_for_account",
    "set_space_flags_for_external",
    "trip",
]
