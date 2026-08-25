"""Canonical owner helpers for plugin state, sessions, config, and grants.

Canonical owner columns on plugin state, sessions, config, grants and queued
work are the only ownership authority. Legacy external keys remain provenance
for Plugin API 2.0, but are never used as an ownership fallback.

plugin_state is Person-or-global: a subject_user_id row requires a live
canonical_person_id; a plugin-global row (no subject) must keep that
shadow NULL. BoundStorageFacade writes global rows and does not isolate
by Person.

plugin_config_values: USER is a live Person, GROUP is a live Space, GLOBAL
keeps both NULL. Two Bindings or SpaceBindings share one logical row.
plugin_background_target_grants: creator is a live Person; target is Person
XOR Space; optional Presence is provenance only. Publication children copy
persisted grant/event canonical fields and never recompute from raw keys.
"""

from __future__ import annotations

from typing import Any, cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel
from qq_ai_bot.identity.canonical_repository import IDENTITY_PLATFORM, optional_external_id
from qq_ai_bot.identity.db_models import (
    CanonicalPersonModel,
    CanonicalSpaceModel,
    IdentityBindingModel,
    PresenceModel,
    SpaceBindingModel,
)
from qq_ai_bot.identity.errors import CanonicalIdentityError
from qq_ai_bot.identity.write_settings import identity_write_settings
from qq_ai_bot.plugin_host.db_models import (
    PluginAgentMessageModel,
    PluginAgentSessionModel,
    PluginBackgroundTargetGrantModel,
    PluginConfigValueModel,
    PluginStateModel,
)

MISSING_CANONICAL_OWNER = "missing_canonical_owner"
CANONICAL_OWNER_DISABLED = "canonical_owner_disabled"
CANONICAL_OWNER_MISMATCH = "canonical_owner_mismatch"
STATE_MISMATCH = "state_mismatch"


class PluginOwnershipError(ValueError):
    """Fail-closed plugin identity error with a sanitized stable category."""

    def __init__(self, category: str, *, message: str | None = None) -> None:
        self.category = category
        super().__init__(message or category)


def _external(raw: str | None) -> str | None:
    if raw is None or not str(raw).strip():
        return None
    return optional_external_id(raw)


def plugin_ownership_error(exc: CanonicalIdentityError) -> PluginOwnershipError:
    """Map an identity fail-closed category without leaking raw ids."""

    if exc.category in {"canonical_kind_mismatch", "canonical_owner_mismatch"}:
        return PluginOwnershipError(CANONICAL_OWNER_MISMATCH)
    if exc.category == "canonical_owner_disabled":
        return PluginOwnershipError(CANONICAL_OWNER_DISABLED)
    if exc.category in {"no_space_binding", "no_person", "no_presence"}:
        return PluginOwnershipError(MISSING_CANONICAL_OWNER)
    return PluginOwnershipError(STATE_MISMATCH)


def _ownership_error(exc: CanonicalIdentityError) -> PluginOwnershipError:
    return plugin_ownership_error(exc)


async def _identity_binding(session: AsyncSession, external: str) -> IdentityBindingModel | None:
    return cast(
        IdentityBindingModel | None,
        await session.scalar(
            select(IdentityBindingModel).where(
                IdentityBindingModel.platform == IDENTITY_PLATFORM,
                IdentityBindingModel.external_account_id == external,
            )
        ),
    )


async def _space_binding(session: AsyncSession, external: str) -> SpaceBindingModel | None:
    return cast(
        SpaceBindingModel | None,
        await session.scalar(
            select(SpaceBindingModel).where(
                SpaceBindingModel.platform == IDENTITY_PLATFORM,
                SpaceBindingModel.external_space_id == external,
            )
        ),
    )


async def _presence_row(session: AsyncSession, external: str) -> PresenceModel | None:
    return cast(
        PresenceModel | None,
        await session.scalar(
            select(PresenceModel).where(
                PresenceModel.platform == IDENTITY_PLATFORM,
                PresenceModel.external_account_id == external,
            )
        ),
    )


async def _bound_person_id(session: AsyncSession, user_id: str | None) -> str | None:
    external = _external(user_id)
    if external is None:
        return None
    if await _presence_row(session, external) is not None:
        return None
    binding = await _identity_binding(session, external)
    if binding is None:
        return None
    return binding.person_id


async def _bound_space_id(session: AsyncSession, group_id: str | None) -> str | None:
    external = _external(group_id)
    if external is None:
        return None
    binding = await _space_binding(session, external)
    if binding is None:
        return None
    return binding.space_id


async def _require_bound_live_person(
    session: AsyncSession,
    external: str,
    *,
    missing_message: str | None,
) -> str:
    if await _presence_row(session, external) is not None:
        raise PluginOwnershipError(CANONICAL_OWNER_MISMATCH)
    try:
        settings = identity_write_settings()
    except CanonicalIdentityError as exc:
        raise _ownership_error(exc) from None
    if external in settings.ignored_bot_users:
        raise PluginOwnershipError(CANONICAL_OWNER_MISMATCH)
    binding = await _identity_binding(session, external)
    if binding is None:
        raise PluginOwnershipError(MISSING_CANONICAL_OWNER, message=missing_message)
    if binding.status != "active":
        raise PluginOwnershipError(CANONICAL_OWNER_DISABLED)
    return await require_live_person(session, binding.person_id)


async def require_live_person(session: AsyncSession, person_id: str | None) -> str:
    """Read a Person by canonical id only. Never consult QQ/group/bot keys."""

    if not person_id:
        raise PluginOwnershipError(MISSING_CANONICAL_OWNER)
    if await session.get(PresenceModel, person_id) is not None:
        raise PluginOwnershipError(CANONICAL_OWNER_MISMATCH)
    if await session.get(CanonicalSpaceModel, person_id) is not None:
        raise PluginOwnershipError(CANONICAL_OWNER_MISMATCH)
    person = await session.get(CanonicalPersonModel, person_id)
    if person is None:
        raise PluginOwnershipError(MISSING_CANONICAL_OWNER)
    if not person.enabled:
        raise PluginOwnershipError(CANONICAL_OWNER_DISABLED)
    return person.id


async def require_live_space(session: AsyncSession, space_id: str | None) -> str:
    """Read a Space by canonical id only. Never consult raw group/bot keys."""

    if not space_id:
        raise PluginOwnershipError(MISSING_CANONICAL_OWNER)
    if await session.get(PresenceModel, space_id) is not None:
        raise PluginOwnershipError(CANONICAL_OWNER_MISMATCH)
    if await session.get(CanonicalPersonModel, space_id) is not None:
        raise PluginOwnershipError(CANONICAL_OWNER_MISMATCH)
    space = await session.get(CanonicalSpaceModel, space_id)
    if space is None:
        raise PluginOwnershipError(MISSING_CANONICAL_OWNER)
    if not space.enabled:
        raise PluginOwnershipError(CANONICAL_OWNER_DISABLED)
    return space.id


async def resolve_human_person_id(
    session: AsyncSession,
    user_id: str | None,
    *,
    missing_message: str | None = None,
) -> str:
    """Map a trusted external account to a Person. Presence/bot never become Person."""

    external = _external(user_id)
    if external is None:
        raise PluginOwnershipError(MISSING_CANONICAL_OWNER, message=missing_message)
    return await _require_bound_live_person(session, external, missing_message=missing_message)


async def resolve_active_space_id(
    session: AsyncSession,
    group_id: str | None,
) -> str:
    """Map a trusted external group to a Space."""

    external = _external(group_id)
    if external is None:
        raise PluginOwnershipError(MISSING_CANONICAL_OWNER)
    binding = await _space_binding(session, external)
    if binding is None:
        raise PluginOwnershipError(MISSING_CANONICAL_OWNER)
    if binding.status != "active":
        raise PluginOwnershipError(CANONICAL_OWNER_DISABLED)
    return await require_live_space(session, binding.space_id)


async def stamp_state_owner(
    session: AsyncSession,
    row: PluginStateModel,
) -> None:
    """Stamp Person onto subject-owned state. Plugin-global stays NULL."""

    subject = row.subject_user_id
    if not subject:
        if row.canonical_person_id:
            raise PluginOwnershipError(STATE_MISMATCH)
        return
    person_id = await resolve_human_person_id(session, subject)
    if row.canonical_person_id not in {None, person_id}:
        raise PluginOwnershipError(CANONICAL_OWNER_MISMATCH)
    row.canonical_person_id = person_id


async def require_state_readable(session: AsyncSession, row: PluginStateModel) -> None:
    """Subject rows need a live Person; global rows stay unowned."""

    if not row.subject_user_id:
        if row.canonical_person_id:
            raise PluginOwnershipError(STATE_MISMATCH)
        return
    await require_live_person(session, row.canonical_person_id)


async def stamp_session_owners(
    session: AsyncSession,
    row: PluginAgentSessionModel,
) -> None:
    """Fill session Person/Space from trusted bindings. Plugin scope stays unowned."""

    if row.scope_type == "plugin":
        if row.canonical_owner_person_id or row.canonical_space_id:
            raise PluginOwnershipError(STATE_MISMATCH)
        return
    if row.scope_type == "user":
        if row.canonical_space_id:
            raise PluginOwnershipError(STATE_MISMATCH)
        row.canonical_owner_person_id = await resolve_human_person_id(
            session,
            row.owner_user_id or row.scope_id,
            missing_message="session owner has no Person",
        )
        return
    row.canonical_space_id = await resolve_active_space_id(session, row.scope_id)
    if row.owner_user_id:
        row.canonical_owner_person_id = await resolve_human_person_id(
            session,
            row.owner_user_id,
            missing_message="session owner has no Person",
        )


async def require_session_readable(session: AsyncSession, row: PluginAgentSessionModel) -> None:
    """Read session ownership from canonical Person/Space columns only."""

    if row.scope_type == "plugin":
        if row.canonical_owner_person_id or row.canonical_space_id:
            raise PluginOwnershipError(STATE_MISMATCH)
        return
    if row.scope_type == "user":
        if row.canonical_space_id:
            raise PluginOwnershipError(STATE_MISMATCH)
        await require_live_person(session, row.canonical_owner_person_id)
        return
    if row.scope_type != "group":
        raise PluginOwnershipError(STATE_MISMATCH)
    await require_live_space(session, row.canonical_space_id)
    if row.owner_user_id and not row.canonical_owner_person_id:
        raise PluginOwnershipError(MISSING_CANONICAL_OWNER)
    if row.canonical_owner_person_id:
        await require_live_person(session, row.canonical_owner_person_id)


async def actor_matches_session(
    session: AsyncSession,
    row: PluginAgentSessionModel,
    *,
    actor_user_id: str,
    current_group_id: str | None,
) -> bool:
    """Compare persisted canonical ownership without revealing row existence."""

    if row.scope_type == "plugin":
        return True
    if row.scope_type == "user":
        actor_person = await _bound_person_id(session, actor_user_id)
        return actor_person is not None and actor_person == row.canonical_owner_person_id
    actor_space = await _bound_space_id(session, current_group_id)
    return actor_space is not None and actor_space == row.canonical_space_id


async def require_live_actor(
    session: AsyncSession,
    row: PluginAgentSessionModel,
    *,
    actor_user_id: str,
    current_group_id: str | None,
) -> None:
    """After identity match, inactive/disabled actor bindings fail closed."""

    if row.scope_type == "user":
        await resolve_human_person_id(
            session,
            actor_user_id,
            missing_message="session owner has no Person",
        )
        return
    if row.scope_type == "group":
        await resolve_active_space_id(session, current_group_id)


async def inherit_message_sender_person(
    session: AsyncSession,
    parent: PluginAgentSessionModel,
    *,
    role: str,
    sender_user_id: str | None,
) -> str | None:
    """Child rows inherit the session Person. Caller IDs cannot override it."""

    if role != "user":
        return None
    await require_session_readable(session, parent)
    if parent.scope_type == "plugin":
        return None
    owner = parent.canonical_owner_person_id
    if owner:
        await require_live_person(session, owner)
    if sender_user_id:
        sender_person = await resolve_human_person_id(
            session,
            sender_user_id,
            missing_message="session sender has no Person",
        )
        if sender_person != owner:
            raise PluginOwnershipError(CANONICAL_OWNER_MISMATCH)
    return owner


def apply_inherited_sender(row: PluginAgentMessageModel, person_id: str | None) -> None:
    """Host-stamped only. There is no caller-supplied canonical setter."""

    row.canonical_sender_person_id = person_id


async def require_live_presence(session: AsyncSession, presence_id: str | None) -> str:
    """Read a Yuki Presence by canonical id only. Never consult bot QQ keys."""

    if not presence_id:
        raise PluginOwnershipError(MISSING_CANONICAL_OWNER)
    if await session.get(CanonicalPersonModel, presence_id) is not None:
        raise PluginOwnershipError(CANONICAL_OWNER_MISMATCH)
    if await session.get(CanonicalSpaceModel, presence_id) is not None:
        raise PluginOwnershipError(CANONICAL_OWNER_MISMATCH)
    presence = await session.get(PresenceModel, presence_id)
    if presence is None:
        raise PluginOwnershipError(MISSING_CANONICAL_OWNER)
    if not presence.enabled:
        raise PluginOwnershipError(CANONICAL_OWNER_DISABLED)
    return presence.id


async def require_live_conversation(
    session: AsyncSession, conversation_id: str | None
) -> CanonicalConversationModel:
    """Read an existing Conversation by canonical id. Do not create one."""

    if not conversation_id:
        raise PluginOwnershipError(MISSING_CANONICAL_OWNER)
    conversation = await session.get(CanonicalConversationModel, conversation_id)
    if conversation is None:
        raise PluginOwnershipError(MISSING_CANONICAL_OWNER)
    return conversation


async def resolve_config_owners(
    session: AsyncSession,
    *,
    scope_type: str,
    scope_id: str,
) -> tuple[str | None, str | None]:
    """Return the live Person/Space pair for one config scope."""

    if scope_type == "global":
        return None, None
    if scope_type == "user":
        return await resolve_human_person_id(session, scope_id), None
    return None, await resolve_active_space_id(session, scope_id)


async def find_config_lineage(
    session: AsyncSession,
    *,
    plugin_id: str,
    scope_type: str,
    key: str,
    person_id: str | None,
    space_id: str | None,
) -> PluginConfigValueModel | None:
    """Locate one logical config row by canonical owner, not raw UNIQUE."""

    statement = select(PluginConfigValueModel).where(
        PluginConfigValueModel.plugin_id == plugin_id,
        PluginConfigValueModel.scope_type == scope_type,
        PluginConfigValueModel.key == key,
    )
    if scope_type == "user":
        statement = statement.where(PluginConfigValueModel.canonical_person_id == person_id)
    elif scope_type == "group":
        statement = statement.where(PluginConfigValueModel.canonical_space_id == space_id)
    rows = list((await session.scalars(statement)).all())
    if scope_type == "global":
        if any(row.canonical_person_id or row.canonical_space_id for row in rows):
            raise PluginOwnershipError(STATE_MISMATCH)
    if len(rows) > 1:
        raise PluginOwnershipError(STATE_MISMATCH)
    if not rows:
        return None
    return rows[0]


async def stamp_config_owners(
    session: AsyncSession,
    row: PluginConfigValueModel,
) -> None:
    """Stamp Person/Space onto scoped config. GLOBAL stays unowned."""

    if row.scope_type == "global":
        if row.canonical_person_id or row.canonical_space_id:
            raise PluginOwnershipError(STATE_MISMATCH)
        row.canonical_person_id = None
        row.canonical_space_id = None
        return
    person_id, space_id = await resolve_config_owners(
        session,
        scope_type=row.scope_type,
        scope_id=row.scope_id,
    )
    if row.scope_type == "user":
        if row.canonical_space_id:
            raise PluginOwnershipError(STATE_MISMATCH)
        if row.canonical_person_id not in {None, person_id}:
            raise PluginOwnershipError(CANONICAL_OWNER_MISMATCH)
        row.canonical_person_id = person_id
        row.canonical_space_id = None
        return
    if row.canonical_person_id:
        raise PluginOwnershipError(STATE_MISMATCH)
    if row.canonical_space_id not in {None, space_id}:
        raise PluginOwnershipError(CANONICAL_OWNER_MISMATCH)
    row.canonical_person_id = None
    row.canonical_space_id = space_id


async def require_config_readable(session: AsyncSession, row: PluginConfigValueModel) -> None:
    """Canonical config: USER Person, GROUP Space, GLOBAL both NULL."""

    if row.scope_type == "global":
        if row.canonical_person_id or row.canonical_space_id:
            raise PluginOwnershipError(STATE_MISMATCH)
        return
    if row.scope_type == "user":
        if row.canonical_space_id:
            raise PluginOwnershipError(STATE_MISMATCH)
        await require_live_person(session, row.canonical_person_id)
        return
    if row.scope_type != "group":
        raise PluginOwnershipError(STATE_MISMATCH)
    if row.canonical_person_id:
        raise PluginOwnershipError(STATE_MISMATCH)
    await require_live_space(session, row.canonical_space_id)


async def resolve_grant_target_owners(
    session: AsyncSession,
    *,
    target_type: str,
    target_id: str,
) -> tuple[str | None, str | None]:
    """Return live Person XOR Space for a grant/publish target."""

    if target_type == "private":
        return await resolve_human_person_id(session, target_id), None
    return None, await resolve_active_space_id(session, target_id)


async def find_grant_lineage(
    session: AsyncSession,
    *,
    plugin_id: str,
    target_type: str,
    target_id: str,
    person_id: str | None,
    space_id: str | None,
) -> PluginBackgroundTargetGrantModel | None:
    """Find one logical grant by Person/Space. Conflicting raw rows fail closed."""

    if target_type == "private":
        canonical_rows = list(
            (
                await session.scalars(
                    select(PluginBackgroundTargetGrantModel).where(
                        PluginBackgroundTargetGrantModel.plugin_id == plugin_id,
                        PluginBackgroundTargetGrantModel.target_type == target_type,
                        PluginBackgroundTargetGrantModel.canonical_target_person_id == person_id,
                    )
                )
            ).all()
        )
    else:
        canonical_rows = list(
            (
                await session.scalars(
                    select(PluginBackgroundTargetGrantModel).where(
                        PluginBackgroundTargetGrantModel.plugin_id == plugin_id,
                        PluginBackgroundTargetGrantModel.target_type == target_type,
                        PluginBackgroundTargetGrantModel.canonical_target_space_id == space_id,
                    )
                )
            ).all()
        )
    raw = await session.scalar(
        select(PluginBackgroundTargetGrantModel).where(
            PluginBackgroundTargetGrantModel.plugin_id == plugin_id,
            PluginBackgroundTargetGrantModel.target_type == target_type,
            PluginBackgroundTargetGrantModel.target_id == target_id,
        )
    )
    if len(canonical_rows) > 1:
        raise PluginOwnershipError(STATE_MISMATCH)
    if canonical_rows:
        chosen = canonical_rows[0]
        if raw is not None and raw.id != chosen.id:
            raise PluginOwnershipError(STATE_MISMATCH)
        return chosen
    if raw is None:
        return None
    if raw.canonical_target_person_id and raw.canonical_target_space_id:
        raise PluginOwnershipError(STATE_MISMATCH)
    if raw.canonical_target_person_id not in {None, person_id}:
        raise PluginOwnershipError(CANONICAL_OWNER_MISMATCH)
    if raw.canonical_target_space_id not in {None, space_id}:
        raise PluginOwnershipError(CANONICAL_OWNER_MISMATCH)
    return raw


async def stamp_grant_owners(
    session: AsyncSession,
    row: PluginBackgroundTargetGrantModel,
    *,
    created_by_user_id: str,
    target_type: str,
    target_id: str,
    bot_user_id: str,
) -> None:
    """Fill grant creator/target/presence. Presence is provenance only."""

    creator_id = await resolve_human_person_id(
        session,
        created_by_user_id,
        missing_message="grant creator is not a known person",
    )
    person_id, space_id = await resolve_grant_target_owners(
        session,
        target_type=target_type,
        target_id=target_id,
    )
    if row.canonical_created_by_person_id not in {None, creator_id}:
        raise PluginOwnershipError(CANONICAL_OWNER_MISMATCH)
    if row.canonical_target_person_id not in {None, person_id}:
        raise PluginOwnershipError(CANONICAL_OWNER_MISMATCH)
    if row.canonical_target_space_id not in {None, space_id}:
        raise PluginOwnershipError(CANONICAL_OWNER_MISMATCH)
    if row.canonical_target_person_id and row.canonical_target_space_id:
        raise PluginOwnershipError(STATE_MISMATCH)
    if (person_id is None) == (space_id is None):
        raise PluginOwnershipError(STATE_MISMATCH)
    from qq_ai_bot.identity.ingress import require_existing_presence

    try:
        presence_id = await require_existing_presence(session, bot_user_id)
    except CanonicalIdentityError as exc:
        raise plugin_ownership_error(exc) from None
    row.canonical_created_by_person_id = creator_id
    row.canonical_target_person_id = person_id
    row.canonical_target_space_id = space_id
    row.canonical_presence_id = presence_id


async def require_grant_readable(
    session: AsyncSession, row: PluginBackgroundTargetGrantModel
) -> None:
    """Require live creator Person, live target XOR, and optional Presence."""

    await require_live_person(session, row.canonical_created_by_person_id)
    person_id = row.canonical_target_person_id
    space_id = row.canonical_target_space_id
    if person_id and space_id:
        raise PluginOwnershipError(STATE_MISMATCH)
    if not person_id and not space_id:
        raise PluginOwnershipError(MISSING_CANONICAL_OWNER)
    if row.target_type == "private":
        if space_id:
            raise PluginOwnershipError(STATE_MISMATCH)
        await require_live_person(session, person_id)
    elif row.target_type == "group":
        if person_id:
            raise PluginOwnershipError(STATE_MISMATCH)
        await require_live_space(session, space_id)
    else:
        raise PluginOwnershipError(STATE_MISMATCH)
    if row.canonical_presence_id:
        await require_live_presence(session, row.canonical_presence_id)


def inherit_publication_canonicals(
    child: Any,
    *,
    grant: PluginBackgroundTargetGrantModel,
    conversation_id: str | None,
    presence_id: str | None,
) -> None:
    """Copy persisted grant/event canonicals. Never recompute from raw keys."""

    child.canonical_target_person_id = grant.canonical_target_person_id
    child.canonical_target_space_id = grant.canonical_target_space_id
    child.canonical_conversation_id = conversation_id
    child.canonical_presence_id = grant.canonical_presence_id or presence_id


async def require_inherited_publication(
    session: AsyncSession,
    child: object,
    *,
    grant: PluginBackgroundTargetGrantModel,
    conversation_id: str | None,
    presence_id: str | None,
) -> None:
    """Fail closed unless children exactly match the persisted parent fields."""

    expected_presence = grant.canonical_presence_id or presence_id
    if (
        getattr(child, "canonical_target_person_id", None) != grant.canonical_target_person_id
        or getattr(child, "canonical_target_space_id", None) != grant.canonical_target_space_id
        or getattr(child, "canonical_conversation_id", None) != conversation_id
        or getattr(child, "canonical_presence_id", None) != expected_presence
    ):
        raise PluginOwnershipError(STATE_MISMATCH)
    if grant.canonical_target_person_id and grant.canonical_target_space_id:
        raise PluginOwnershipError(STATE_MISMATCH)
    if not grant.canonical_target_person_id and not grant.canonical_target_space_id:
        raise PluginOwnershipError(MISSING_CANONICAL_OWNER)
    conversation = await require_live_conversation(session, conversation_id)
    if grant.canonical_target_person_id:
        if conversation.person_id != grant.canonical_target_person_id or conversation.space_id:
            raise PluginOwnershipError(CANONICAL_OWNER_MISMATCH)
    elif conversation.space_id != grant.canonical_target_space_id or conversation.person_id:
        raise PluginOwnershipError(CANONICAL_OWNER_MISMATCH)
    if expected_presence:
        await require_live_presence(session, expected_presence)
