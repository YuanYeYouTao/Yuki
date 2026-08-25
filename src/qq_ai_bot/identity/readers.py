"""Gated v2 readers. v1 callers keep reading legacy people/groups."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from qq_ai_bot.identity.db_models import (
    CanonicalPersonModel,
    CanonicalSpaceModel,
    IdentityBindingModel,
    SpaceBindingModel,
)
from qq_ai_bot.identity.inventory import IDENTITY_PLATFORM
from qq_ai_bot.identity.runtime import identity_runtime_is_complete_v2
from qq_ai_bot.services.policies import EffectiveGroupPolicy, EffectivePrivatePolicy


async def v2_private_policy(
    session: AsyncSession,
    user_id: str,
    *,
    fallback: EffectivePrivatePolicy,
) -> EffectivePrivatePolicy:
    if not await identity_runtime_is_complete_v2(session):
        return fallback
    binding = await session.scalar(
        select(IdentityBindingModel).where(
            IdentityBindingModel.platform == IDENTITY_PLATFORM,
            IdentityBindingModel.external_account_id == user_id,
        )
    )
    if binding is None:
        return fallback
    person = await session.get(CanonicalPersonModel, binding.person_id)
    if person is None:
        return fallback
    return EffectivePrivatePolicy(enabled=bool(person.enabled))


async def v2_group_policy(
    session: AsyncSession,
    group_id: str,
    *,
    fallback: EffectiveGroupPolicy,
) -> EffectiveGroupPolicy:
    if not await identity_runtime_is_complete_v2(session):
        return fallback
    binding = await session.scalar(
        select(SpaceBindingModel).where(
            SpaceBindingModel.platform == IDENTITY_PLATFORM,
            SpaceBindingModel.external_space_id == group_id,
        )
    )
    if binding is None:
        return fallback
    space = await session.get(CanonicalSpaceModel, binding.space_id)
    if space is None:
        return fallback
    return EffectiveGroupPolicy(
        enabled=bool(space.enabled),
        require_mention=bool(space.require_mention),
        autonomous_enabled=bool(space.autonomous_enabled),
    )
