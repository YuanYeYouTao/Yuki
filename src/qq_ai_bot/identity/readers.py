"""Canonical Person and Space policy readers."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from qq_ai_bot.identity.canonical_repository import (
    find_identity_binding,
)
from qq_ai_bot.identity.db_models import (
    CanonicalPersonModel,
    CanonicalSpaceModel,
)
from qq_ai_bot.services.policies import EffectiveGroupPolicy, EffectivePrivatePolicy


async def canonical_private_policy(
    session: AsyncSession,
    user_id: str,
    *,
    platform: str = "qq",
) -> EffectivePrivatePolicy:
    binding = await find_identity_binding(session, user_id, platform=platform)
    if binding is None or binding.status != "active":
        return EffectivePrivatePolicy(enabled=False)
    person = await session.get(CanonicalPersonModel, binding.person_id)
    if person is None:
        return EffectivePrivatePolicy(enabled=False)
    return EffectivePrivatePolicy(enabled=bool(person.enabled))


async def canonical_group_policy(
    session: AsyncSession,
    space_id: str,
) -> EffectiveGroupPolicy:
    space = await session.get(CanonicalSpaceModel, space_id)
    if space is None:
        return EffectiveGroupPolicy(enabled=False)
    return EffectiveGroupPolicy(
        enabled=bool(space.enabled),
        require_mention=bool(space.require_mention),
        autonomous_enabled=bool(space.autonomous_enabled),
    )
