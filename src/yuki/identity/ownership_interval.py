"""The history interval belongs to this binding row, including after transfer."""

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from yuki.identity.db_models import IdentityBindingModel


@dataclass(frozen=True)
class BindingOwnership:
    binding_id: str
    account: str
    starts_at: datetime | None
    ends_at: datetime | None
    platform: str = "qq"

    @classmethod
    def of(cls, binding: IdentityBindingModel) -> "BindingOwnership":
        return cls(
            binding.id,
            binding.external_account_id,
            binding.history_starts_at,
            binding.updated_at if binding.status == "transferred" else None,
            binding.platform,
        )

    @property
    def bounded(self) -> bool:
        return self.starts_at is not None or self.ends_at is not None

    def contains(self, timestamp: datetime) -> bool:
        def utc(value: datetime) -> datetime:
            return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)

        point = utc(timestamp)
        return (self.starts_at is None or point >= utc(self.starts_at)) and (
            self.ends_at is None or point < utc(self.ends_at)
        )


async def historical_person_ids(
    session: AsyncSession,
    platform: str,
    accounts: tuple[str, ...],
    at: datetime,
) -> dict[str, str]:
    """Read the original human owners at an event's time, including transferred accounts."""

    if not accounts:
        return {}
    binding = IdentityBindingModel
    return dict(
        (
            await session.execute(
                select(binding.external_account_id, binding.person_id).where(
                    binding.platform == platform,
                    binding.external_account_id.in_(accounts),
                    or_(binding.history_starts_at.is_(None), binding.history_starts_at <= at),
                    or_(binding.status != "transferred", binding.updated_at > at),
                )
            )
        )
        .tuples()
        .all()
    )
