"""Async persistence dual-write for canonical identity shadows.

Callers pass their existing AsyncSession. This module never opens sqlite3
connections, never imports CLI/renderer, and never scans the whole database
like C7 backfill. Unique races use a SAVEPOINT, then re-read.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Literal, cast
from uuid import uuid4

from sqlalchemy import select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from qq_ai_bot.conversation.canonical_db_models import (
    CanonicalConversationModel,
    ConversationLegacyAliasModel,
    PersonActiveRouteModel,
)
from qq_ai_bot.conversation.rollup.db_models import ConversationScopeModel
from qq_ai_bot.identity.backfill_types import AccountEvidence
from qq_ai_bot.identity.canonical_extension_schema import C6_EXTENSION_INVENTORY
from qq_ai_bot.identity.canonical_ownership_schema import C5_OWNERSHIP_INVENTORY
from qq_ai_bot.identity.classifier import classify_account
from qq_ai_bot.identity.db_models import (
    CanonicalPersonModel,
    CanonicalSpaceModel,
    IdentityBindingModel,
    IdentityRuntimeStateModel,
    PresenceModel,
    SpaceBindingModel,
)
from qq_ai_bot.identity.errors import IdentityDualWriteError
from qq_ai_bot.identity.inventory import IDENTITY_PLATFORM
from qq_ai_bot.identity.runtime import (
    identity_runtime_is_complete_v2,
    require_identity_runtime,
)
from qq_ai_bot.identity.sanitize import normalize_external_id
from qq_ai_bot.identity.write_settings import identity_write_settings
from qq_ai_bot.persistence.models import (
    ChatEventModel,
    GroupModel,
    MembershipModel,
    PersonAliasModel,
    PersonModel,
)

AccountRole = Literal["human", "yuki_self", "external_bot", "private_peer"]
Failpoint = Callable[[str], None]

_failpoint: Failpoint | None = None


def set_identity_failpoint(failpoint: Failpoint | None) -> None:
    global _failpoint
    _failpoint = failpoint


def trip(name: str) -> None:
    if _failpoint is not None:
        _failpoint(name)


def _new_id() -> str:
    return str(uuid4())


def _external_id(raw: str) -> str:
    normalized = normalize_external_id(raw)
    if normalized is None:
        raise IdentityDualWriteError("unclassified")
    return normalized


async def require_v1_runtime(session: AsyncSession) -> None:
    """Fail closed unless the singleton identity epoch is exactly v1."""

    rows = (
        await session.scalars(
            select(IdentityRuntimeStateModel).order_by(IdentityRuntimeStateModel.id)
        )
    ).all()
    if len(rows) != 1 or int(rows[0].id) != 1 or str(rows[0].state) != "v1":
        raise IdentityDualWriteError("identity_runtime_state")


async def _binding_for(session: AsyncSession, external_id: str) -> IdentityBindingModel | None:
    return cast(
        IdentityBindingModel | None,
        await session.scalar(
            select(IdentityBindingModel).where(
                IdentityBindingModel.platform == IDENTITY_PLATFORM,
                IdentityBindingModel.external_account_id == external_id,
            )
        ),
    )


async def _presence_for(session: AsyncSession, external_id: str) -> PresenceModel | None:
    return cast(
        PresenceModel | None,
        await session.scalar(
            select(PresenceModel).where(
                PresenceModel.platform == IDENTITY_PLATFORM,
                PresenceModel.external_account_id == external_id,
            )
        ),
    )


async def _space_binding_for(session: AsyncSession, group_id: str) -> SpaceBindingModel | None:
    return cast(
        SpaceBindingModel | None,
        await session.scalar(
            select(SpaceBindingModel).where(
                SpaceBindingModel.platform == IDENTITY_PLATFORM,
                SpaceBindingModel.external_space_id == group_id,
            )
        ),
    )


def _classify(
    *,
    external_id: str,
    role: AccountRole | None,
    is_bot: bool,
    people: PersonModel | None,
    binding: IdentityBindingModel | None,
    presence: PresenceModel | None,
) -> str:
    settings = identity_write_settings()
    yuki_self = role == "yuki_self"
    # The current event's bot_user_id is ingress Presence even if the same
    # number also appears in SUPERUSERS or IGNORED_BOT_USERS.
    ignored = (not yuki_self) and external_id in settings.ignored_bot_users
    superuser = (not yuki_self) and external_id in settings.superusers
    people_is_bot = bool(people is not None and people.is_bot)
    people_human = (
        people is not None and not people.is_bot and not ignored and not yuki_self and not is_bot
    )
    evidence = AccountEvidence(
        external_id=external_id,
        sources=frozenset({"people.user_id"}),
        yuki_self=yuki_self or (presence is not None and binding is None and not ignored),
        ignored_bot=ignored,
        legacy_is_bot=is_bot or people_is_bot,
        human_sender=role == "human",
        private_peer=role == "private_peer",
        member=False,
        superuser=superuser,
        people_human=people_human,
        supporting_person=False,
        strong_person=superuser or people_human,
        nickname=people.nickname if people is not None else "",
        existing_person_id=people.canonical_person_id if people is not None else None,
        existing_binding_person_id=binding.person_id if binding is not None else None,
        existing_presence_id=presence.id if presence is not None else None,
        shadow_person_ids=frozenset(
            [people.canonical_person_id]
            if people is not None and people.canonical_person_id
            else []
        ),
        shadow_presence_ids=frozenset(),
    )
    classification, category = classify_account(evidence)
    if classification == "conflict":
        raise IdentityDualWriteError(category or "unclassified")
    return classification


async def _assign_people_shadow(
    people: PersonModel,
    person_id: str | None,
) -> None:
    current = people.canonical_person_id
    if person_id is None:
        if current is not None:
            raise IdentityDualWriteError("canonical_kind_mismatch")
        return
    if current is None:
        people.canonical_person_id = person_id
        return
    if current != person_id:
        raise IdentityDualWriteError("canonical_owner_mismatch")


async def _create_person_binding(
    session: AsyncSession,
    *,
    external_id: str,
    display_name: str,
    now: datetime,
) -> IdentityBindingModel:
    try:
        async with session.begin_nested():
            person = CanonicalPersonModel(
                id=_new_id(),
                enabled=True,
                revision=1,
                created_at=now,
                updated_at=now,
            )
            binding = IdentityBindingModel(
                id=_new_id(),
                person_id=person.id,
                platform=IDENTITY_PLATFORM,
                external_account_id=external_id,
                display_name=display_name[:128],
                status="active",
                revision=1,
                created_at=now,
                updated_at=now,
            )
            session.add(person)
            session.add(binding)
            await session.flush()
            return binding
    except IntegrityError:
        existing = await _binding_for(session, external_id)
        if existing is None:
            raise IdentityDualWriteError("canonical_kind_mismatch") from None
        return existing


async def _create_presence(
    session: AsyncSession,
    *,
    external_id: str,
    now: datetime,
) -> PresenceModel:
    try:
        async with session.begin_nested():
            presence = PresenceModel(
                id=_new_id(),
                platform=IDENTITY_PLATFORM,
                external_account_id=external_id,
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
        existing = await _presence_for(session, external_id)
        if existing is None:
            raise IdentityDualWriteError("canonical_kind_mismatch") from None
        return existing


async def _create_space_binding(
    session: AsyncSession,
    *,
    group_id: str,
    name: str,
    enabled: bool,
    autonomous_enabled: bool,
    require_mention: bool,
    now: datetime,
) -> SpaceBindingModel:
    try:
        async with session.begin_nested():
            space = CanonicalSpaceModel(
                id=_new_id(),
                name=name[:128],
                enabled=enabled,
                autonomous_enabled=autonomous_enabled,
                require_mention=require_mention,
                revision=1,
                created_at=now,
                updated_at=now,
            )
            binding = SpaceBindingModel(
                id=_new_id(),
                space_id=space.id,
                platform=IDENTITY_PLATFORM,
                external_space_id=group_id,
                display_name=name[:128],
                status="active",
                revision=1,
                created_at=now,
                updated_at=now,
            )
            session.add(space)
            session.add(binding)
            await session.flush()
            return binding
    except IntegrityError:
        existing = await _space_binding_for(session, group_id)
        if existing is None:
            raise IdentityDualWriteError("canonical_kind_mismatch") from None
        return existing


async def ensure_canonical_person_preconfig(
    session: AsyncSession,
    user_id: str,
    *,
    now: datetime,
) -> str:
    """Create Person+Binding for Settings preconfig. Never inserts a people row."""

    await require_identity_runtime(session, allowed=frozenset({"v1", "v2"}))
    external_id = _external_id(user_id)
    binding = await _binding_for(session, external_id)
    if binding is not None:
        return binding.person_id
    created = await _create_person_binding(
        session,
        external_id=external_id,
        display_name="",
        now=now,
    )
    return created.person_id


async def ensure_canonical_space_preconfig(
    session: AsyncSession,
    group_id: str,
    *,
    now: datetime,
) -> str:
    """Create Space+Binding for Settings preconfig. Never inserts a groups row."""

    await require_identity_runtime(session, allowed=frozenset({"v1", "v2"}))
    external_id = _external_id(group_id)
    binding = await _space_binding_for(session, external_id)
    if binding is not None:
        return binding.space_id
    created = await _create_space_binding(
        session,
        group_id=external_id,
        name="",
        enabled=True,
        autonomous_enabled=True,
        require_mention=True,
        now=now,
    )
    return created.space_id


async def sync_account(
    session: AsyncSession,
    user_id: str,
    *,
    role: AccountRole | None = None,
    is_bot: bool = False,
    display_name: str = "",
    now: datetime,
) -> str | None:
    """Reuse or create Person/Presence. Returns person_id, presence_id, or None.

    Never inserts a people row. Fills people.canonical_person_id only for Person.
    """

    await require_v1_runtime(session)
    external_id = _external_id(user_id)
    people = await session.get(PersonModel, external_id)
    binding = await _binding_for(session, external_id)
    presence = await _presence_for(session, external_id)
    classification = _classify(
        external_id=external_id,
        role=role,
        is_bot=is_bot,
        people=people,
        binding=binding,
        presence=presence,
    )
    if classification == "person":
        if presence is not None:
            raise IdentityDualWriteError("canonical_kind_mismatch")
        if binding is None:
            binding = await _create_person_binding(
                session,
                external_id=external_id,
                display_name=display_name or (people.nickname if people is not None else ""),
                now=now,
            )
            trip("after_identity_foundation")
        if people is not None:
            await _assign_people_shadow(people, binding.person_id)
        return binding.person_id
    if classification == "yuki_presence":
        if binding is not None:
            raise IdentityDualWriteError("canonical_kind_mismatch")
        if people is not None:
            await _assign_people_shadow(people, None)
        if presence is None:
            presence = await _create_presence(session, external_id=external_id, now=now)
            trip("after_identity_foundation")
        return presence.id
    if people is not None:
        await _assign_people_shadow(people, None)
    return None


async def sync_presence(
    session: AsyncSession,
    bot_user_id: str,
    *,
    now: datetime,
) -> str:
    """Create or reuse Presence for an explicit Yuki bot_user_id.

    Never inserts a people row. Callers that do not need a legacy bot
    people row must use this instead of _ensure_person.
    """

    presence_id = await sync_account(
        session,
        bot_user_id,
        role="yuki_self",
        is_bot=True,
        now=now,
    )
    if presence_id is None:
        raise IdentityDualWriteError("canonical_kind_mismatch")
    from qq_ai_bot.gateway.registry import process_registry

    registry = process_registry()
    if registry is not None:
        registry.bind_presence(
            platform=IDENTITY_PLATFORM,
            external_account_id=_external_id(bot_user_id),
            presence_id=presence_id,
        )
    return presence_id


async def sync_space(
    session: AsyncSession,
    group_id: str,
    *,
    name: str = "",
    enabled: bool = True,
    autonomous_enabled: bool = True,
    require_mention: bool = True,
    now: datetime,
) -> str:
    """Reuse or create Space+SpaceBinding. Never inserts a groups row."""

    await require_v1_runtime(session)
    external_id = _external_id(group_id)
    group = await session.get(GroupModel, external_id)
    binding = await _space_binding_for(session, external_id)
    if binding is None:
        binding = await _create_space_binding(
            session,
            group_id=external_id,
            name=name or (group.name if group is not None else ""),
            enabled=enabled if group is None else bool(group.enabled),
            autonomous_enabled=(
                autonomous_enabled if group is None else bool(group.autonomous_enabled)
            ),
            require_mention=require_mention if group is None else bool(group.require_mention),
            now=now,
        )
        trip("after_identity_foundation")
    if group is not None:
        current = group.canonical_space_id
        if current is None:
            group.canonical_space_id = binding.space_id
        elif current != binding.space_id:
            raise IdentityDualWriteError("canonical_owner_mismatch")
    return binding.space_id


def _shadow_conflict(current: str | None, proven: str | None) -> None:
    if proven is None or current is None or current == proven:
        return
    raise IdentityDualWriteError("canonical_owner_mismatch")


async def sync_person_enabled(session: AsyncSession, user_id: str, enabled: bool) -> None:
    await require_identity_runtime(session, allowed=frozenset({"v1", "v2"}))
    people = await session.get(PersonModel, _external_id(user_id))
    person_id = people.canonical_person_id if people is not None else None
    if person_id is None:
        binding = await _binding_for(session, _external_id(user_id))
        person_id = None if binding is None else binding.person_id
    if person_id is None:
        return
    person = await session.get(CanonicalPersonModel, person_id)
    if person is None:
        return
    person.enabled = enabled
    person.updated_at = datetime.now(UTC)
    person.revision = int(person.revision) + 1


async def sync_space_flags(
    session: AsyncSession,
    group_id: str,
    *,
    enabled: bool | None = None,
    autonomous_enabled: bool | None = None,
    require_mention: bool | None = None,
) -> None:
    await require_identity_runtime(session, allowed=frozenset({"v1", "v2"}))
    group = await session.get(GroupModel, _external_id(group_id))
    space_id = group.canonical_space_id if group is not None else None
    if space_id is None:
        binding = await _space_binding_for(session, _external_id(group_id))
        space_id = None if binding is None else binding.space_id
    if space_id is None:
        return
    space = await session.get(CanonicalSpaceModel, space_id)
    if space is None:
        return
    if enabled is not None:
        space.enabled = enabled
    if autonomous_enabled is not None:
        space.autonomous_enabled = autonomous_enabled
    if require_mention is not None:
        space.require_mention = require_mention
    space.updated_at = datetime.now(UTC)
    space.revision = int(space.revision) + 1


async def ensure_runtime_people_row(
    session: AsyncSession,
    user_id: str,
    *,
    nickname: str = "",
    is_bot: bool = False,
    now: datetime | None = None,
) -> PersonModel:
    """Insert the legacy people carrier on complete v2. Does not call require_v1."""

    await require_identity_runtime(session, allowed=frozenset({"v2"}))
    timestamp = now or datetime.now(UTC)
    external_id = _external_id(user_id)
    person = await session.get(PersonModel, external_id)
    if person is None:
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        await session.execute(
            sqlite_insert(PersonModel)
            .values(
                user_id=external_id,
                nickname=nickname[:128],
                enabled=True,
                is_bot=is_bot,
                first_seen_at=timestamp,
                last_seen_at=timestamp,
            )
            .on_conflict_do_nothing(index_elements=["user_id"])
        )
        person = await session.get(PersonModel, external_id)
        if person is None:
            raise IdentityDualWriteError("unclassified")
    if nickname:
        person.nickname = nickname[:128]
    person.is_bot = person.is_bot or is_bot
    person.last_seen_at = timestamp
    binding = await _binding_for(session, external_id)
    if binding is not None and person.canonical_person_id is None:
        person.canonical_person_id = binding.person_id
    return person


async def ensure_runtime_group_row(
    session: AsyncSession,
    group_id: str,
    *,
    name: str = "",
    now: datetime | None = None,
) -> GroupModel:
    await require_identity_runtime(session, allowed=frozenset({"v2"}))
    timestamp = now or datetime.now(UTC)
    external_id = _external_id(group_id)
    group = await session.get(GroupModel, external_id)
    if group is None:
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        await session.execute(
            sqlite_insert(GroupModel)
            .values(
                group_id=external_id,
                name=name[:128],
                enabled=True,
                require_mention=True,
                autonomous_enabled=True,
                first_seen_at=timestamp,
                last_seen_at=timestamp,
                updated_at=timestamp,
            )
            .on_conflict_do_nothing(index_elements=["group_id"])
        )
        group = await session.get(GroupModel, external_id)
        if group is None:
            raise IdentityDualWriteError("unclassified")
    binding = await _space_binding_for(session, external_id)
    if binding is not None and group.canonical_space_id is None:
        group.canonical_space_id = binding.space_id
    return group


async def fill_membership_shadows(
    session: AsyncSession,
    user_id: str,
    group_id: str,
) -> None:
    """Fill C5 membership shadows when this write proves both owners."""

    await require_identity_runtime(session, allowed=frozenset({"v1", "v2"}))
    membership = await session.get(MembershipModel, {"user_id": user_id, "group_id": group_id})
    if membership is None:
        return
    people = await session.get(PersonModel, user_id)
    group = await session.get(GroupModel, group_id)
    person_id = people.canonical_person_id if people is not None else None
    space_id = group.canonical_space_id if group is not None else None
    _shadow_conflict(membership.canonical_person_id, person_id)
    _shadow_conflict(membership.canonical_space_id, space_id)
    if person_id is not None and membership.canonical_person_id is None:
        membership.canonical_person_id = person_id
    if space_id is not None and membership.canonical_space_id is None:
        membership.canonical_space_id = space_id


async def fill_alias_shadows(
    session: AsyncSession,
    user_id: str,
    group_scope: str,
    alias: str,
) -> None:
    """Fill C5 alias shadows when this write proves owners."""

    await require_v1_runtime(session)
    row = await session.scalar(
        select(PersonAliasModel).where(
            PersonAliasModel.user_id == user_id,
            PersonAliasModel.group_scope == group_scope,
            PersonAliasModel.alias == alias,
        )
    )
    if row is None:
        return
    people = await session.get(PersonModel, user_id)
    person_id = people.canonical_person_id if people is not None else None
    space_id = None
    if group_scope:
        group = await session.get(GroupModel, group_scope)
        space_id = group.canonical_space_id if group is not None else None
    _shadow_conflict(row.canonical_person_id, person_id)
    _shadow_conflict(row.canonical_space_id, space_id)
    if person_id is not None and row.canonical_person_id is None:
        row.canonical_person_id = person_id
    if space_id is not None and row.canonical_space_id is None:
        row.canonical_space_id = space_id


async def apply_event_identity_shadows(
    session: AsyncSession,
    event: ChatEventModel,
    *,
    sender_is_bot: bool,
) -> None:
    """Fill event identity shadows. Conversation/event/receipt stay NULL.

    v1 still reads people. Complete v2 uses only existing Presence/Binding.
    Incomplete or missing runtime still fail-closes via require_v1_runtime.
    """

    if await identity_runtime_is_complete_v2(session):
        await _fill_v2_event_identity_shadows(session, event, sender_is_bot=sender_is_bot)
        return
    await require_v1_runtime(session)
    trip("before_event_shadow")
    sender_id = _external_id(event.sender_user_id)
    bot_id = _external_id(event.bot_user_id)
    settings = identity_write_settings()
    sender = await session.get(PersonModel, sender_id)
    ingress = await _presence_for(session, bot_id)
    event.canonical_event_id = None
    event.canonical_conversation_id = None
    event.utterance_fingerprint = None
    event.suppression_status = None
    event.ingress_provider = None
    event.ingress_gateway_instance_id = None
    event.ingress_presence_id = ingress.id if ingress is not None else None
    if event.event_kind == "external_event" or event.direction == "external":
        event.author_kind = "system"
        event.author_person_id = None
        event.author_presence_id = None
        return
    if sender_id == bot_id:
        if ingress is None:
            raise IdentityDualWriteError("unclassified")
        event.author_kind = "yuki"
        event.author_presence_id = ingress.id
        event.author_person_id = None
        return
    ignored = sender_id in settings.ignored_bot_users
    legacy_bot = sender_is_bot or bool(sender is not None and sender.is_bot)
    if ignored or legacy_bot:
        event.author_kind = "external_bot"
        event.author_person_id = None
        event.author_presence_id = None
        return
    person_id = sender.canonical_person_id if sender is not None else None
    if person_id is None:
        raise IdentityDualWriteError("unclassified")
    event.author_kind = "person"
    event.author_person_id = person_id
    event.author_presence_id = None


async def _fill_v2_event_identity_shadows(
    session: AsyncSession,
    event: ChatEventModel,
    *,
    sender_is_bot: bool,
) -> None:
    """Canonical-only author fill. Never reads or writes people/groups."""

    trip("before_event_shadow")
    sender_id = _external_id(event.sender_user_id)
    bot_id = _external_id(event.bot_user_id)
    settings = identity_write_settings()
    ingress = await _presence_for(session, bot_id)
    event.canonical_event_id = None
    event.canonical_conversation_id = None
    event.utterance_fingerprint = None
    event.suppression_status = None
    event.ingress_provider = None
    event.ingress_gateway_instance_id = None
    event.ingress_presence_id = ingress.id if ingress is not None else None
    if event.event_kind == "external_event" or event.direction == "external":
        event.author_kind = "system"
        event.author_person_id = None
        event.author_presence_id = None
        return
    if sender_id == bot_id:
        if ingress is None:
            raise IdentityDualWriteError("unclassified")
        event.author_kind = "yuki"
        event.author_presence_id = ingress.id
        event.author_person_id = None
        return
    if sender_is_bot or sender_id in settings.ignored_bot_users:
        event.author_kind = "external_bot"
        event.author_person_id = None
        event.author_presence_id = None
        return
    binding = await _binding_for(session, sender_id)
    if binding is None or binding.status != "active":
        raise IdentityDualWriteError("unclassified")
    person = await session.get(CanonicalPersonModel, binding.person_id)
    if person is None or not person.enabled:
        raise IdentityDualWriteError("unclassified")
    event.author_kind = "person"
    event.author_person_id = person.id
    event.author_presence_id = None


def _person_fk_targets() -> tuple[tuple[str, str], ...]:
    items: list[tuple[str, str]] = []
    for spec in (*C5_OWNERSHIP_INVENTORY, *C6_EXTENSION_INVENTORY):
        for column in spec["columns"]:
            if column["parent_table"] == "persons":
                items.append((spec["table"], column["column"]))
    items.append(("chat_events", "author_person_id"))
    return tuple(items)


async def forget_canonical_for_external_account(session: AsyncSession, user_id: str) -> None:
    """Clear Person-level canonical ownership then delete Bindings/Person.

    Does not delete Presence, Space, or another Person. Unknown leftover FKs
    fail the surrounding transaction instead of half-deleting.
    """

    await require_identity_runtime(session, allowed=frozenset({"v1", "v2"}))
    trip("before_forget_canonical")
    external_id = _external_id(user_id)
    binding = await _binding_for(session, external_id)
    if binding is None:
        people = await session.get(PersonModel, external_id)
        if people is not None and people.canonical_person_id is not None:
            raise IdentityDualWriteError("canonical_owner_mismatch")
        return
    person_id = binding.person_id
    bindings = (
        await session.scalars(
            select(IdentityBindingModel).where(IdentityBindingModel.person_id == person_id)
        )
    ).all()
    for item in bindings:
        if item.external_account_id == external_id:
            continue
        if await session.get(PersonModel, item.external_account_id) is not None:
            raise IdentityDualWriteError("forgetme_multiple_bindings")
    leftover_people = (
        await session.scalars(
            select(PersonModel.user_id).where(
                PersonModel.canonical_person_id == person_id,
                PersonModel.user_id != external_id,
            )
        )
    ).all()
    if leftover_people:
        raise IdentityDualWriteError("forgetme_multiple_bindings")
    for table, column in _person_fk_targets():
        exists = await session.scalar(
            text("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = :name"),
            {"name": table},
        )
        if exists is None:
            continue
        if table == "chat_events" and column == "author_person_id":
            await session.execute(
                update(ChatEventModel)
                .where(ChatEventModel.author_person_id == person_id)
                .values(author_kind=None, author_person_id=None)
            )
            continue
        await session.execute(
            text(f'UPDATE "{table}" SET "{column}" = NULL WHERE "{column}" = :person_id'),
            {"person_id": person_id},
        )
    session.expire_all()
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
            update(ConversationScopeModel)
            .where(ConversationScopeModel.canonical_conversation_id == conversation.id)
            .values(canonical_conversation_id=None)
        )
        await session.execute(
            update(ChatEventModel)
            .where(ChatEventModel.canonical_conversation_id == conversation.id)
            .values(canonical_conversation_id=None, canonical_event_id=None)
        )
        aliases = (
            await session.scalars(
                select(ConversationLegacyAliasModel).where(
                    ConversationLegacyAliasModel.conversation_id == conversation.id
                )
            )
        ).all()
        for alias in aliases:
            await session.delete(alias)
        await session.delete(conversation)
    route = await session.get(PersonActiveRouteModel, person_id)
    if route is not None:
        await session.delete(route)
    person = await session.get(CanonicalPersonModel, person_id)
    if person is not None:
        await session.delete(person)
    await session.flush()
