"""Resolve an existing configuration Space without execution-state policy."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from yuki.identity.db_models import CanonicalSpaceModel
from yuki.identity.errors import CanonicalIdentityError


async def resolve_space_owner(session: AsyncSession, space_id: str | None) -> str:
    owner = await session.scalar(
        select(CanonicalSpaceModel.id).where(CanonicalSpaceModel.id == space_id)
    )
    if owner is None:
        raise CanonicalIdentityError("missing_canonical_owner")
    return owner
