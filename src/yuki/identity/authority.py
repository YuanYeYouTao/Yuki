"""Current authority belongs to the permanent Person, independently of accounts."""

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from yuki.identity.db_models import CanonicalPersonModel


async def person_is_superuser(session: AsyncSession, person_id: str | None) -> bool:
    """Read the current qualification without reviving a disabled original source."""
    if person_id is None:
        return False
    original, root = aliased(CanonicalPersonModel), aliased(CanonicalPersonModel)
    return bool(
        await session.scalar(
            select(root.is_superuser)
            .select_from(original)
            .join(root, root.id == func.coalesce(original.merged_into_person_id, original.id))
            .where(
                original.id == person_id,
                original.enabled.is_(True),
                root.enabled.is_(True),
            )
        )
    )
