"""Trusted temporal normalization for Memory V2 claims."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from yuki.memory.enums import MemoryTemporalMode


@dataclass(frozen=True, slots=True)
class ResolvedMemoryTemporalRange:
    mode: MemoryTemporalMode
    valid_from: datetime | None
    valid_until: datetime | None


class MemoryTemporalResolver:
    """Resolve model-provided ISO values against the trusted event clock."""

    def resolve(
        self,
        *,
        mode: MemoryTemporalMode,
        valid_from: str | datetime | None,
        valid_until: str | datetime | None,
        occurred_at: datetime,
        timezone_name: str = "Asia/Shanghai",
    ) -> ResolvedMemoryTemporalRange:
        zone = ZoneInfo(timezone_name)
        event_time = self._aware(occurred_at, zone)
        start = self._parse(valid_from, zone)
        end = self._parse(valid_until, zone)
        if mode is MemoryTemporalMode.PERSISTENT:
            end = None
        elif mode is MemoryTemporalMode.TEMPORARY and end is None:
            raise ValueError("temporary memory requires valid_until")
        elif mode is MemoryTemporalMode.EPISODE and start is None:
            start = event_time
        if start is not None and end is not None and start > end:
            raise ValueError("memory valid_from must not be after valid_until")
        return ResolvedMemoryTemporalRange(mode, start, end)

    @staticmethod
    def _parse(value: str | datetime | None, zone: ZoneInfo) -> datetime | None:
        if value is None:
            return None
        if isinstance(value, datetime):
            return MemoryTemporalResolver._aware(value, zone)
        normalized = value.strip()
        if not normalized:
            return None
        try:
            parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("memory time must be ISO 8601") from exc
        return MemoryTemporalResolver._aware(parsed, zone)

    @staticmethod
    def _aware(value: datetime, zone: ZoneInfo) -> datetime:
        if value.tzinfo is None:
            value = value.replace(tzinfo=zone)
        return value.astimezone(UTC)
