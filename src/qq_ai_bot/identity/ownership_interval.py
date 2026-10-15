"""The history interval belongs to this binding row, including after transfer."""

from dataclasses import dataclass
from datetime import UTC, datetime

from qq_ai_bot.identity.db_models import IdentityBindingModel


@dataclass(frozen=True)
class BindingOwnership:
    binding_id: str
    account: str
    starts_at: datetime | None
    ends_at: datetime | None

    @classmethod
    def of(cls, binding: IdentityBindingModel) -> "BindingOwnership":
        return cls(
            binding.id,
            binding.external_account_id,
            binding.history_starts_at,
            binding.updated_at if binding.status == "transferred" else None,
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
