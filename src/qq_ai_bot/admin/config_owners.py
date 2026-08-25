"""Canonical owner keys for runtime configuration."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from qq_ai_bot.identity.errors import CanonicalIdentityError
from qq_ai_bot.persistence.models import RuntimeConfigOverrideModel
from qq_ai_bot.services.canonical_owners import resolve_live_person_id, resolve_live_space_id

CANONICAL_OWNER_MISMATCH = "canonical_owner_mismatch"


@dataclass(frozen=True, slots=True)
class UserConfigScope:
    person_id: str
    storage_scope_id: str


@dataclass(frozen=True, slots=True)
class GroupConfigScope:
    space_id: str
    storage_scope_id: str


async def resolve_user_config_scope(session: AsyncSession, scope_id: str) -> UserConfigScope:
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
        raise CanonicalIdentityError(CANONICAL_OWNER_MISMATCH)
    return UserConfigScope(person_id, next(iter(storage_ids), person_id))


async def resolve_group_config_scope(session: AsyncSession, scope_id: str) -> GroupConfigScope:
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
        raise CanonicalIdentityError(CANONICAL_OWNER_MISMATCH)
    return GroupConfigScope(space_id, next(iter(storage_ids), space_id))
