"""Canonical owner helpers for plugin state, sessions, config, and grants.

Canonical owner columns on plugin state, sessions, config, grants and queued
work are the only ownership authority.  QQ and group identifiers enter only at
trusted adapter boundaries, are resolved before the first flush, and are never
stored as ownership keys.

plugin_state is Person-or-global: a canonical_person_id identifies the owner;
a plugin-global row keeps it NULL. BoundStorageFacade writes global rows and
does not isolate by Person.

plugin_config_values: USER is a live Person, GROUP is a live Space, GLOBAL
keeps both NULL. Two Bindings or SpaceBindings share one logical row.
plugin_background_target_grants: creator is a live Person; target is Person
XOR Space; optional Presence is provenance only. Publication children copy
persisted grant/event canonical fields and never recompute from raw keys.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel
from qq_ai_bot.identity.db_models import (
    CanonicalPersonModel,
    CanonicalSpaceModel,
    PresenceModel,
)
from qq_ai_bot.identity.errors import CanonicalIdentityError
from qq_ai_bot.identity.person_identity import (
    current_person_id,
    person_family_condition,
    same_person_id,
)
from qq_ai_bot.plugin_host.db_models import (
    PluginAgentSessionModel,
    PluginBackgroundTargetGrantModel,
    PluginConfigValueModel,
    PluginStateModel,
)
from yuki_plugin_sdk.errors import PluginPermissionError

MISSING_CANONICAL_OWNER = "missing_canonical_owner"
CANONICAL_OWNER_DISABLED = "canonical_owner_disabled"
CANONICAL_OWNER_MISMATCH = "canonical_owner_mismatch"
STATE_MISMATCH = "state_mismatch"


class PluginOwnershipError(ValueError):
    """Fail-closed plugin identity error with a sanitized stable category."""

    def __init__(self, category: str, *, message: str | None = None) -> None:
        self.category = category
        super().__init__(message or category)


def plugin_ownership_error(exc: CanonicalIdentityError) -> PluginOwnershipError:
    """Map an identity fail-closed category without leaking raw ids."""

    if exc.category in {"canonical_kind_mismatch", "canonical_owner_mismatch"}:
        return PluginOwnershipError(CANONICAL_OWNER_MISMATCH)
    if exc.category == "canonical_owner_disabled":
        return PluginOwnershipError(CANONICAL_OWNER_DISABLED)
    if exc.category in {"no_space_binding", "no_person", "no_presence"}:
        return PluginOwnershipError(MISSING_CANONICAL_OWNER)
    return PluginOwnershipError(STATE_MISMATCH)


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
    root_id = await current_person_id(session, person.id)
    root = await session.get(CanonicalPersonModel, root_id)
    if root is None or not root.enabled:
        raise PluginOwnershipError(CANONICAL_OWNER_DISABLED)
    return root_id


async def require_live_people(session: AsyncSession, person_ids: tuple[str, ...]) -> None:
    """Apply the same Person facts once for a state directory's distinct owners."""
    if not person_ids:
        return
    presences = set(
        await session.scalars(select(PresenceModel.id).where(PresenceModel.id.in_(person_ids)))
    )
    spaces = set(
        await session.scalars(
            select(CanonicalSpaceModel.id).where(CanonicalSpaceModel.id.in_(person_ids))
        )
    )
    people = {
        person_id: enabled
        for person_id, enabled in (
            await session.execute(
                select(CanonicalPersonModel.id, CanonicalPersonModel.enabled).where(
                    CanonicalPersonModel.id.in_(person_ids)
                )
            )
        )
    }
    for person_id in person_ids:
        if person_id in presences or person_id in spaces:
            raise PluginOwnershipError(CANONICAL_OWNER_MISMATCH)
        if person_id not in people:
            raise PluginOwnershipError(MISSING_CANONICAL_OWNER)
        if not people[person_id]:
            raise PluginOwnershipError(CANONICAL_OWNER_DISABLED)
    roots = list(
        await session.scalars(
            select(CanonicalPersonModel.merged_into_person_id).where(
                CanonicalPersonModel.id.in_(person_ids),
                CanonicalPersonModel.merged_into_person_id.is_not(None),
            )
        )
    )
    if (
        roots
        and await session.scalar(
            select(CanonicalPersonModel.id)
            .where(CanonicalPersonModel.id.in_(roots), CanonicalPersonModel.enabled.is_(False))
            .limit(1)
        )
        is not None
    ):
        raise PluginOwnershipError(CANONICAL_OWNER_DISABLED)


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
    """Accept an already resolved Person; an account alias is not an owner."""
    if user_id is None:
        raise PluginOwnershipError(MISSING_CANONICAL_OWNER, message=missing_message)
    return await require_live_person(session, user_id)


async def resolve_active_space_id(
    session: AsyncSession,
    group_id: str | None,
) -> str:
    """Accept an already resolved Space, independent of its transport bindings."""
    return await require_live_space(session, group_id)


async def resolve_state_owner(
    session: AsyncSession,
    subject_user_id: str | None,
) -> str | None:
    """Resolve an optional transport subject before persisting plugin state."""

    if not subject_user_id:
        return None
    return await resolve_human_person_id(session, subject_user_id)


async def require_state_readable(session: AsyncSession, row: PluginStateModel) -> None:
    """Person rows need a live owner; global rows stay unowned."""

    if not row.canonical_person_id:
        return
    await require_live_person(session, row.canonical_person_id)


async def resolve_session_owners(
    session: AsyncSession,
    *,
    scope_type: str,
    owner_user_id: str | None,
    scope_id: str,
) -> tuple[str | None, str | None]:
    """Resolve a session's strict canonical owner shape before insertion."""

    if scope_type == "plugin":
        return None, None
    if scope_type == "user":
        user_owner = await resolve_human_person_id(
            session,
            owner_user_id or scope_id,
            missing_message="session owner has no Person",
        )
        scoped = await resolve_human_person_id(
            session,
            scope_id,
            missing_message="session owner has no Person",
        )
        if user_owner != scoped:
            raise PluginOwnershipError(CANONICAL_OWNER_MISMATCH)
        return user_owner, None
    if scope_type != "group":
        raise PluginOwnershipError(STATE_MISMATCH)
    group_owner: str | None = None
    if owner_user_id:
        group_owner = await resolve_human_person_id(
            session,
            owner_user_id,
            missing_message="session actor has no Person",
        )
    return group_owner, await resolve_active_space_id(session, scope_id)


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
    if row.canonical_owner_person_id:
        await require_live_person(session, row.canonical_owner_person_id)


async def actor_matches_session(
    session: AsyncSession,
    row: PluginAgentSessionModel,
    *,
    actor_user_id: str | None,
    current_group_id: str | None,
) -> bool:
    """Compare persisted canonical ownership without revealing row existence."""

    if row.scope_type == "plugin":
        return True
    if row.scope_type == "user":
        return bool(
            actor_user_id
            and row.canonical_owner_person_id
            and await same_person_id(session, actor_user_id, row.canonical_owner_person_id)
        )
    if current_group_id != row.canonical_space_id:
        return False
    if not row.canonical_owner_person_id:
        return True
    return bool(
        actor_user_id
        and await same_person_id(session, actor_user_id, row.canonical_owner_person_id)
    )


async def require_live_actor(
    session: AsyncSession,
    row: PluginAgentSessionModel,
    *,
    actor_user_id: str | None,
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
        if actor_user_id is not None:
            await resolve_human_person_id(
                session, actor_user_id, missing_message="session actor has no Person"
            )
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
    if sender_user_id is None and parent.canonical_owner_person_id is None:
        return None
    try:
        sender_person = await resolve_human_person_id(
            session,
            sender_user_id,
            missing_message="session sender has no Person",
        )
    except PluginOwnershipError as exc:
        # Plugin-global transcripts may include an external bot or another
        # transport actor that is intentionally not a Person.  Its raw sender
        # remains immutable provenance; it never becomes an authorization key.
        if parent.scope_type == "plugin" and exc.category in {
            MISSING_CANONICAL_OWNER,
            CANONICAL_OWNER_MISMATCH,
        }:
            return None
        raise
    if parent.scope_type == "user" and (
        parent.canonical_owner_person_id is None
        or not await same_person_id(session, sender_person, parent.canonical_owner_person_id)
    ):
        raise PluginOwnershipError(CANONICAL_OWNER_MISMATCH)
    if (
        parent.scope_type == "group"
        and parent.canonical_owner_person_id
        and not await same_person_id(session, sender_person, parent.canonical_owner_person_id)
    ):
        raise PluginOwnershipError(CANONICAL_OWNER_MISMATCH)
    return sender_person


async def require_presence_provenance(session: AsyncSession, presence_id: str | None) -> str:
    """Validate immutable Presence provenance without making it a send gate.

    A historical Presence may be disabled after an account/provider switch.
    Delivery resolves the current Person/Space route and must not be blocked by
    that historical lifecycle state.
    """

    if not presence_id:
        raise PluginOwnershipError(MISSING_CANONICAL_OWNER)
    if await session.get(CanonicalPersonModel, presence_id) is not None:
        raise PluginOwnershipError(CANONICAL_OWNER_MISMATCH)
    if await session.get(CanonicalSpaceModel, presence_id) is not None:
        raise PluginOwnershipError(CANONICAL_OWNER_MISMATCH)
    presence = await session.get(PresenceModel, presence_id)
    if presence is None:
        raise PluginOwnershipError(MISSING_CANONICAL_OWNER)
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
    person_id: str | None,
    space_id: str | None,
    conversation_id: str | None = None,
) -> PluginBackgroundTargetGrantModel | None:
    """Locate the original entry grant; an ambiguous owner needs its Conversation."""
    statement = select(PluginBackgroundTargetGrantModel).where(
        PluginBackgroundTargetGrantModel.plugin_id == plugin_id,
        PluginBackgroundTargetGrantModel.target_type == target_type,
        (
            person_family_condition(
                PluginBackgroundTargetGrantModel.canonical_target_person_id, person_id
            )
            if target_type == "private"
            else PluginBackgroundTargetGrantModel.canonical_target_space_id == space_id
        ),
    )
    if conversation_id is not None:
        statement = statement.where(
            or_(
                PluginBackgroundTargetGrantModel.canonical_conversation_id == conversation_id,
                PluginBackgroundTargetGrantModel.canonical_conversation_id.is_(None),
            )
        )
    rows = list(await session.scalars(statement.limit(2)))
    if conversation_id is not None:
        exact = [row for row in rows if row.canonical_conversation_id == conversation_id]
        if exact:
            rows = exact
    if len(rows) > 1:
        raise PluginPermissionError(
            "notification target has multiple entries; specify conversation_id"
        )
    return rows[0] if rows else None


async def stamp_grant_owners(
    session: AsyncSession,
    row: PluginBackgroundTargetGrantModel,
    *,
    created_by_user_id: str,
    target_type: str,
    target_id: str,
    presence_id: str,
    conversation_id: str,
) -> None:
    """Persist the original canonical entry and grant authority."""
    creator_id = await resolve_human_person_id(session, created_by_user_id)
    person_id, space_id = await resolve_grant_target_owners(
        session, target_type=target_type, target_id=target_id
    )
    conversation = await require_live_conversation(session, conversation_id)
    person_matches = conversation.person_id == person_id or bool(
        conversation.person_id
        and person_id
        and await same_person_id(session, conversation.person_id, person_id)
    )
    if not person_matches or conversation.space_id != space_id:
        raise PluginOwnershipError(CANONICAL_OWNER_MISMATCH)
    await require_presence_provenance(session, presence_id)
    if row.canonical_created_by_person_id is not None and not await same_person_id(
        session, row.canonical_created_by_person_id, creator_id
    ):
        raise PluginOwnershipError(CANONICAL_OWNER_MISMATCH)
    if row.canonical_target_person_id is not None and (
        person_id is None
        or not await same_person_id(session, row.canonical_target_person_id, person_id)
    ):
        raise PluginOwnershipError(CANONICAL_OWNER_MISMATCH)
    if row.canonical_target_space_id not in {None, space_id}:
        raise PluginOwnershipError(CANONICAL_OWNER_MISMATCH)
    if row.canonical_conversation_id not in {None, conversation_id}:
        raise PluginOwnershipError(CANONICAL_OWNER_MISMATCH)
    row.canonical_created_by_person_id = row.canonical_created_by_person_id or creator_id
    row.canonical_target_person_id = row.canonical_target_person_id or person_id
    row.canonical_target_space_id = space_id
    row.canonical_presence_id = presence_id
    row.canonical_conversation_id = conversation_id


async def require_grant_readable(
    session: AsyncSession, row: PluginBackgroundTargetGrantModel
) -> None:
    """Require canonical authority only for an enabled, executable grant."""

    if not row.enabled:
        if row.canonical_target_person_id and row.canonical_target_space_id:
            raise PluginOwnershipError(STATE_MISMATCH)
        return

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
    await require_presence_provenance(session, row.canonical_presence_id)


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
        if (
            not conversation.person_id
            or not await same_person_id(
                session, conversation.person_id, grant.canonical_target_person_id
            )
            or conversation.space_id
        ):
            raise PluginOwnershipError(CANONICAL_OWNER_MISMATCH)
    elif conversation.space_id != grant.canonical_target_space_id or conversation.person_id:
        raise PluginOwnershipError(CANONICAL_OWNER_MISMATCH)
    await require_presence_provenance(session, expected_presence)
