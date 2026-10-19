"""The history interval belongs to this binding row, including after transfer."""

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from yuki.identity.db_models import AccountAssignmentModel, AccountModel, IdentityBindingModel


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
            binding.ended_at,
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
    known = list(
        await session.scalars(
            select(AccountModel).where(
                AccountModel.platform == platform, AccountModel.external_account_id.in_(accounts)
            )
        )
    )
    stages = await assignments_at(session, tuple(account.id for account in known), at)
    return {
        account.external_account_id: stage.person_id
        for account in known
        if (stage := stages.get(account.id)) is not None
        and stage.kind == "person"
        and stage.person_id
    }


async def assignments_at(
    session: AsyncSession,
    account_ids: tuple[str, ...],
    at: datetime | None,
) -> dict[str, AccountAssignmentModel | None]:
    """Read actual stages in batches; None means a provably earlier unknown owner.

    An absent key has no current or proved historical relation. No source time
    means normal current admission, never a fabricated historical timestamp.
    """
    if not account_ids:
        return {}
    current = list(
        await session.scalars(
            select(AccountAssignmentModel).where(
                AccountAssignmentModel.account_id.in_(account_ids),
                AccountAssignmentModel.status != "ended",
            )
        )
    )
    result: dict[str, AccountAssignmentModel | None] = {row.account_id: row for row in current}
    if at is None:
        return result
    point = at.replace(tzinfo=UTC) if at.tzinfo is None else at.astimezone(UTC)
    earlier = set(account_ids) - result.keys()
    for row in current:
        start = row.started_at or row.first_seen_at
        if point < (start.replace(tzinfo=UTC) if start.tzinfo is None else start.astimezone(UTC)):
            result[row.account_id] = None
            earlier.add(row.account_id)
    if earlier:
        known = await session.execute(
            select(
                AccountAssignmentModel.account_id,
                func.count(),
                func.count(AccountAssignmentModel.ended_at),
                func.max(AccountAssignmentModel.ended_at),
            )
            .where(AccountAssignmentModel.account_id.in_(earlier))
            .group_by(AccountAssignmentModel.account_id)
        )
        for account_id, total, known_ends, latest_end in known:
            ended = (
                latest_end.replace(tzinfo=UTC)
                if latest_end is not None and latest_end.tzinfo is None
                else latest_end
            )
            # A new message after every proven ended stage may register a new
            # Person. A historical gap or an unknown end never borrows an owner.
            if account_id in result or total != known_ends or ended is None or point < ended:
                result[account_id] = None
        historic = await session.scalars(
            select(AccountAssignmentModel).where(
                AccountAssignmentModel.account_id.in_(earlier),
                func.coalesce(
                    AccountAssignmentModel.started_at, AccountAssignmentModel.first_seen_at
                )
                <= point,
                AccountAssignmentModel.ended_at > point,
            )
        )
        matched: set[str] = set()
        for row in historic:
            if row.account_id in matched:
                result[row.account_id] = None
            else:
                result[row.account_id] = row
                matched.add(row.account_id)
    return result
