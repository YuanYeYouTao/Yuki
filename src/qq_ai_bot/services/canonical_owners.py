"""Resolve external QQ identifiers to live canonical owners."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from qq_ai_bot.identity.canonical_repository import (
    assert_same_shadow,
    find_identity_binding,
    find_presence,
    find_space_binding,
    optional_external_id,
)
from qq_ai_bot.identity.db_models import CanonicalPersonModel, CanonicalSpaceModel, PresenceModel
from qq_ai_bot.identity.errors import CanonicalIdentityError

MISSING_CANONICAL_OWNER = "missing_canonical_owner"
CANONICAL_OWNER_DISABLED = "canonical_owner_disabled"
CANONICAL_KIND_MISMATCH = "canonical_kind_mismatch"


def _external(raw: str | None) -> str | None:
    return optional_external_id(raw)


async def require_live_person(session: AsyncSession, person_id: str | None) -> str:
    if not person_id:
        raise CanonicalIdentityError(MISSING_CANONICAL_OWNER)
    if (
        await session.get(PresenceModel, person_id) is not None
        or await session.get(CanonicalSpaceModel, person_id) is not None
    ):
        raise CanonicalIdentityError(CANONICAL_KIND_MISMATCH)
    person = await session.get(CanonicalPersonModel, person_id)
    if person is None:
        raise CanonicalIdentityError(MISSING_CANONICAL_OWNER)
    if not person.enabled:
        raise CanonicalIdentityError(CANONICAL_OWNER_DISABLED)
    return person.id


async def require_live_space(session: AsyncSession, space_id: str | None) -> str:
    if not space_id:
        raise CanonicalIdentityError(MISSING_CANONICAL_OWNER)
    if (
        await session.get(PresenceModel, space_id) is not None
        or await session.get(CanonicalPersonModel, space_id) is not None
    ):
        raise CanonicalIdentityError(CANONICAL_KIND_MISMATCH)
    space = await session.get(CanonicalSpaceModel, space_id)
    if space is None:
        raise CanonicalIdentityError(MISSING_CANONICAL_OWNER)
    if not space.enabled:
        raise CanonicalIdentityError(CANONICAL_OWNER_DISABLED)
    return space.id


async def resolve_live_person_id(session: AsyncSession, raw: str | None) -> str:
    external = _external(raw)
    if external is None:
        raise CanonicalIdentityError(MISSING_CANONICAL_OWNER)
    if await session.get(CanonicalPersonModel, external) is not None:
        return await require_live_person(session, external)
    if (
        await session.get(PresenceModel, external) is not None
        or await session.get(CanonicalSpaceModel, external) is not None
        or await find_presence(session, external) is not None
    ):
        raise CanonicalIdentityError(CANONICAL_KIND_MISMATCH)
    binding = await find_identity_binding(session, external)
    if binding is None:
        raise CanonicalIdentityError(MISSING_CANONICAL_OWNER)
    if binding.status != "active":
        raise CanonicalIdentityError(CANONICAL_OWNER_DISABLED)
    return await require_live_person(session, binding.person_id)


async def resolve_live_space_id(session: AsyncSession, raw: str | None) -> str:
    external = _external(raw)
    if external is None:
        raise CanonicalIdentityError(MISSING_CANONICAL_OWNER)
    if await session.get(CanonicalSpaceModel, external) is not None:
        return await require_live_space(session, external)
    if (
        await session.get(PresenceModel, external) is not None
        or await session.get(CanonicalPersonModel, external) is not None
    ):
        raise CanonicalIdentityError(CANONICAL_KIND_MISMATCH)
    binding = await find_space_binding(session, external)
    if binding is None:
        raise CanonicalIdentityError(MISSING_CANONICAL_OWNER)
    if binding.status != "active":
        raise CanonicalIdentityError(CANONICAL_OWNER_DISABLED)
    return await require_live_space(session, binding.space_id)


async def try_live_person_id(session: AsyncSession, raw: str | None) -> str | None:
    if _external(raw) is None:
        return None
    try:
        return await resolve_live_person_id(session, raw)
    except CanonicalIdentityError:
        return None


async def try_live_space_id(session: AsyncSession, raw: str | None) -> str | None:
    if _external(raw) is None:
        return None
    try:
        return await resolve_live_space_id(session, raw)
    except CanonicalIdentityError:
        return None


def stamp_canonical_owners(
    row: object,
    *,
    person_attr: str | None,
    space_attr: str | None,
    person_id: str | None,
    space_id: str | None,
) -> None:
    """Assign already-resolved owners and reject a conflicting correlation."""

    if person_attr is not None and hasattr(row, person_attr):
        current = getattr(row, person_attr)
        assert_same_shadow(current, person_id)
        if current is None:
            setattr(row, person_attr, person_id)
    if space_attr is not None and hasattr(row, space_attr):
        current = getattr(row, space_attr)
        assert_same_shadow(current, space_id)
        if current is None:
            setattr(row, space_attr, space_id)
