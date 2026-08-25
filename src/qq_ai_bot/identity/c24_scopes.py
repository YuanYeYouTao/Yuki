"""C24 complete-v2 read/write gates for Runtime Config and Emoji scopes.

Resolves a supplied external account or group to exactly one live Person or
Space. complete-v2 callers query only canonical_* columns. Raw QQ/group ids
stay provenance. This module never inserts Person, Space, people, or groups.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from qq_ai_bot.identity.db_models import (
    CanonicalPersonModel,
    CanonicalSpaceModel,
    IdentityBindingModel,
    PresenceModel,
    SpaceBindingModel,
)
from qq_ai_bot.identity.errors import IdentityDualWriteError
from qq_ai_bot.identity.inventory import IDENTITY_PLATFORM
from qq_ai_bot.identity.runtime import require_complete_v2_runtime
from qq_ai_bot.identity.sanitize import normalize_external_id
from qq_ai_bot.identity.shadows import assign_shadow
from qq_ai_bot.persistence.models import RuntimeConfigOverrideModel

MISSING_CANONICAL_OWNER = "missing_canonical_owner"
AMBIGUOUS_OWNER = "ambiguous_owner"
CANONICAL_OWNER_DISABLED = "canonical_owner_disabled"
CANONICAL_KIND_MISMATCH = "canonical_kind_mismatch"
CANONICAL_OWNER_MISMATCH = "canonical_owner_mismatch"


@dataclass(frozen=True, slots=True)
class C24UserScope:
    """One live Person used as a user-scoped runtime-config owner."""

    person_id: str
    storage_scope_id: str


@dataclass(frozen=True, slots=True)
class C24GroupScope:
    """One live Space used as a group-scoped runtime-config or emoji owner."""

    space_id: str
    storage_scope_id: str


def _external(raw: str | None) -> str | None:
    if raw is None or not str(raw).strip():
        return None
    return normalize_external_id(str(raw))


async def require_live_person(session: AsyncSession, person_id: str | None) -> str:
    """Read a Person by canonical id only. Presence/Space ids are wrong-kind."""

    if not person_id:
        raise IdentityDualWriteError(MISSING_CANONICAL_OWNER)
    if await session.get(PresenceModel, person_id) is not None:
        raise IdentityDualWriteError(CANONICAL_KIND_MISMATCH)
    if await session.get(CanonicalSpaceModel, person_id) is not None:
        raise IdentityDualWriteError(CANONICAL_KIND_MISMATCH)
    person = await session.get(CanonicalPersonModel, person_id)
    if person is None:
        raise IdentityDualWriteError(MISSING_CANONICAL_OWNER)
    if not person.enabled:
        raise IdentityDualWriteError(CANONICAL_OWNER_DISABLED)
    return person.id


async def require_live_space(session: AsyncSession, space_id: str | None) -> str:
    """Read a Space by canonical id only. Person/Presence ids are wrong-kind."""

    if not space_id:
        raise IdentityDualWriteError(MISSING_CANONICAL_OWNER)
    if await session.get(PresenceModel, space_id) is not None:
        raise IdentityDualWriteError(CANONICAL_KIND_MISMATCH)
    if await session.get(CanonicalPersonModel, space_id) is not None:
        raise IdentityDualWriteError(CANONICAL_KIND_MISMATCH)
    space = await session.get(CanonicalSpaceModel, space_id)
    if space is None:
        raise IdentityDualWriteError(MISSING_CANONICAL_OWNER)
    if not space.enabled:
        raise IdentityDualWriteError(CANONICAL_OWNER_DISABLED)
    return space.id


async def resolve_live_person_id(session: AsyncSession, raw: str | None) -> str:
    """Map a supplied external account or Person id to one live Person."""

    await require_complete_v2_runtime(session)
    external = _external(raw)
    if external is None:
        raise IdentityDualWriteError(MISSING_CANONICAL_OWNER)
    if await session.get(CanonicalPersonModel, external) is not None:
        return await require_live_person(session, external)
    if await session.get(PresenceModel, external) is not None:
        raise IdentityDualWriteError(CANONICAL_KIND_MISMATCH)
    if await session.get(CanonicalSpaceModel, external) is not None:
        raise IdentityDualWriteError(CANONICAL_KIND_MISMATCH)
    bindings = list(
        await session.scalars(
            select(IdentityBindingModel).where(
                IdentityBindingModel.platform == IDENTITY_PLATFORM,
                IdentityBindingModel.external_account_id == external,
            )
        )
    )
    presences = list(
        await session.scalars(
            select(PresenceModel).where(
                PresenceModel.platform == IDENTITY_PLATFORM,
                PresenceModel.external_account_id == external,
            )
        )
    )
    if presences:
        raise IdentityDualWriteError(CANONICAL_KIND_MISMATCH)
    if not bindings:
        raise IdentityDualWriteError(MISSING_CANONICAL_OWNER)
    active = [row for row in bindings if row.status == "active"]
    if len(active) > 1:
        raise IdentityDualWriteError(AMBIGUOUS_OWNER)
    if not active:
        raise IdentityDualWriteError(CANONICAL_OWNER_DISABLED)
    persons = {row.person_id for row in active}
    if len(persons) != 1:
        raise IdentityDualWriteError(AMBIGUOUS_OWNER)
    return await require_live_person(session, active[0].person_id)


async def resolve_live_space_id(session: AsyncSession, raw: str | None) -> str:
    """Map a supplied external group or Space id to one live Space."""

    await require_complete_v2_runtime(session)
    external = _external(raw)
    if external is None:
        raise IdentityDualWriteError(MISSING_CANONICAL_OWNER)
    if await session.get(CanonicalSpaceModel, external) is not None:
        return await require_live_space(session, external)
    if await session.get(CanonicalPersonModel, external) is not None:
        raise IdentityDualWriteError(CANONICAL_KIND_MISMATCH)
    if await session.get(PresenceModel, external) is not None:
        raise IdentityDualWriteError(CANONICAL_KIND_MISMATCH)
    bindings = list(
        await session.scalars(
            select(SpaceBindingModel).where(
                SpaceBindingModel.platform == IDENTITY_PLATFORM,
                SpaceBindingModel.external_space_id == external,
            )
        )
    )
    if not bindings:
        raise IdentityDualWriteError(MISSING_CANONICAL_OWNER)
    active = [row for row in bindings if row.status == "active"]
    if len(active) > 1:
        raise IdentityDualWriteError(AMBIGUOUS_OWNER)
    if not active:
        raise IdentityDualWriteError(CANONICAL_OWNER_DISABLED)
    spaces = {row.space_id for row in active}
    if len(spaces) != 1:
        raise IdentityDualWriteError(AMBIGUOUS_OWNER)
    return await require_live_space(session, active[0].space_id)


async def try_live_person_id(session: AsyncSession, raw: str | None) -> str | None:
    """Stamp first-seen Person when uniquely live. Any resolution failure stays null."""

    if _external(raw) is None:
        return None
    try:
        return await resolve_live_person_id(session, raw)
    except IdentityDualWriteError:
        return None


async def try_live_space_id(session: AsyncSession, raw: str | None) -> str | None:
    """Stamp first-seen Space when uniquely live. Any resolution failure stays null."""

    if _external(raw) is None:
        return None
    try:
        return await resolve_live_space_id(session, raw)
    except IdentityDualWriteError:
        return None


async def resolve_c24_user_config_scope(session: AsyncSession, scope_id: str) -> C24UserScope:
    """Resolve a user write/read to one live Person. Storage is Person-keyed."""

    person_id = await resolve_live_person_id(session, scope_id)
    rows = list(
        await session.scalars(
            select(RuntimeConfigOverrideModel).where(
                RuntimeConfigOverrideModel.scope_type == "user",
                RuntimeConfigOverrideModel.canonical_person_id == person_id,
            )
        )
    )
    storage_ids = {row.scope_id for row in rows}
    if len(storage_ids) > 1:
        raise IdentityDualWriteError(CANONICAL_OWNER_MISMATCH)
    return C24UserScope(
        person_id=person_id,
        storage_scope_id=next(iter(storage_ids), person_id),
    )


async def resolve_c24_group_config_scope(session: AsyncSession, scope_id: str) -> C24GroupScope:
    """Resolve a group write/read to one live Space. Storage is Space-keyed."""

    space_id = await resolve_live_space_id(session, scope_id)
    rows = list(
        await session.scalars(
            select(RuntimeConfigOverrideModel).where(
                RuntimeConfigOverrideModel.scope_type == "group",
                RuntimeConfigOverrideModel.canonical_space_id == space_id,
            )
        )
    )
    storage_ids = {row.scope_id for row in rows}
    if len(storage_ids) > 1:
        raise IdentityDualWriteError(CANONICAL_OWNER_MISMATCH)
    return C24GroupScope(
        space_id=space_id,
        storage_scope_id=next(iter(storage_ids), space_id),
    )


async def stamp_v2_person_space(
    row: object,
    *,
    person_attr: str | None,
    space_attr: str | None,
    person_id: str | None,
    space_id: str | None,
) -> None:
    """Assign already-resolved live owners. Never consult raw QQ/group keys."""

    if person_attr is not None and hasattr(row, person_attr):
        current = getattr(row, person_attr)
        setattr(row, person_attr, await assign_shadow(current, person_id))
    if space_attr is not None and hasattr(row, space_attr):
        current = getattr(row, space_attr)
        setattr(row, space_attr, await assign_shadow(current, space_id))


__all__ = [
    "AMBIGUOUS_OWNER",
    "CANONICAL_KIND_MISMATCH",
    "CANONICAL_OWNER_DISABLED",
    "CANONICAL_OWNER_MISMATCH",
    "MISSING_CANONICAL_OWNER",
    "C24GroupScope",
    "C24UserScope",
    "require_live_person",
    "require_live_space",
    "resolve_c24_group_config_scope",
    "resolve_c24_user_config_scope",
    "resolve_live_person_id",
    "resolve_live_space_id",
    "stamp_v2_person_space",
    "try_live_person_id",
    "try_live_space_id",
]
