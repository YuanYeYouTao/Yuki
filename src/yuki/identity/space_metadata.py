"""Observe native names on existing shared-space identities."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from yuki.domain.profiles import sanitize_profile_name
from yuki.identity.db_models import CanonicalSpaceModel, PresenceModel, SpaceBindingModel
from yuki.persistence.database import Database


async def observe_space_name(
    database: Database,
    *,
    presence_id: str,
    space_binding_id: str,
    name: str | None,
) -> bool:
    """Use an authenticated platform observation without changing admission or routes."""

    observed = sanitize_profile_name(name or "")
    if not observed:
        return False

    async def apply(session: AsyncSession) -> bool:
        row = (
            await session.execute(
                select(SpaceBindingModel, CanonicalSpaceModel)
                .join(CanonicalSpaceModel, CanonicalSpaceModel.id == SpaceBindingModel.space_id)
                .join(
                    PresenceModel,
                    (PresenceModel.id == presence_id)
                    & (PresenceModel.platform == SpaceBindingModel.platform),
                )
                .where(SpaceBindingModel.id == space_binding_id)
            )
        ).one_or_none()
        if row is None:
            return False
        binding, space = row
        if binding.display_name == observed and space.name == observed:
            return False
        now = datetime.now(UTC)
        if binding.display_name != observed:
            binding.display_name = observed
            binding.updated_at = now
            binding.revision += 1
        if space.name != observed:
            space.name = observed
            space.updated_at = now
            space.revision += 1
        return True

    return await database.apply_snapshot_write(apply)
