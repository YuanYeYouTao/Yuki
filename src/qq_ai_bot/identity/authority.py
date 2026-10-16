"""Current Person authority derived from explicitly configured QQ bindings."""

from collections.abc import Collection

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from qq_ai_bot.identity.db_models import CanonicalPersonModel, IdentityBindingModel
from qq_ai_bot.identity.person_identity import person_family_condition, person_root_id


async def person_is_superuser(
    session: AsyncSession, person_id: str | None, superusers: Collection[str]
) -> bool:
    """A linked App/TG account shares its Person's actual QQ administrator grant."""
    if person_id is None or not superusers:
        return False
    return (
        await session.scalar(
            select(IdentityBindingModel.id)
            .join(CanonicalPersonModel, CanonicalPersonModel.id == IdentityBindingModel.person_id)
            .where(
                person_family_condition(CanonicalPersonModel.id, person_id),
                select(CanonicalPersonModel.enabled)
                .where(CanonicalPersonModel.id == person_root_id(person_id))
                .scalar_subquery()
                .is_(True),
                CanonicalPersonModel.enabled.is_(True),
                IdentityBindingModel.platform == "qq",
                IdentityBindingModel.status == "active",
                IdentityBindingModel.external_account_id.in_(superusers),
            )
            .limit(1)
        )
        is not None
    )
