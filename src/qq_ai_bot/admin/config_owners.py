"""Canonical owner keys for runtime configuration."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from qq_ai_bot.services.canonical_owners import resolve_live_person_id, resolve_live_space_id


@dataclass(frozen=True, slots=True)
class UserConfigScope:
    person_id: str

    @property
    def storage_scope_id(self) -> str:
        """Canonical storage owner retained as a call-site compatibility name."""

        return self.person_id


@dataclass(frozen=True, slots=True)
class GroupConfigScope:
    space_id: str

    @property
    def storage_scope_id(self) -> str:
        """Canonical storage owner retained as a call-site compatibility name."""

        return self.space_id


async def resolve_user_config_scope(session: AsyncSession, scope_id: str) -> UserConfigScope:
    person_id = await resolve_live_person_id(session, scope_id)
    return UserConfigScope(person_id)


async def resolve_group_config_scope(session: AsyncSession, scope_id: str) -> GroupConfigScope:
    space_id = await resolve_live_space_id(session, scope_id)
    return GroupConfigScope(space_id)
