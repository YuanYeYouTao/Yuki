"""Trusted clock access and a shared default timezone."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from qq_ai_bot.time.models import TimeContext


class Clock(Protocol):
    """Injectable UTC clock used by chat, scheduling, and deterministic tests."""

    def now(self) -> datetime:
        """Return an aware UTC timestamp."""


class SystemClock:
    """Production clock backed by the host's UTC wall clock."""

    def now(self) -> datetime:
        return datetime.now(UTC)


def validate_timezone(value: str) -> str:
    """Normalize and verify an IANA timezone identifier."""

    normalized = value.strip()
    try:
        ZoneInfo(normalized)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"无效的 IANA 时区：{normalized}") from exc
    return normalized


class TimeContextService:
    """Resolve trusted current time without personal timezone state."""

    def __init__(
        self,
        *,
        default_timezone: str = "Asia/Shanghai",
        clock: Clock | None = None,
    ) -> None:
        self._default_timezone = validate_timezone(default_timezone)
        self._clock = clock or SystemClock()

    @property
    def clock(self) -> Clock:
        return self._clock

    @property
    def default_timezone(self) -> str:
        return self._default_timezone

    def current_in_timezone(self, timezone: str) -> TimeContext:
        """Refresh the clock using an explicitly selected timezone."""

        return self.at(self._utc_now(), timezone)

    def current_default(self) -> TimeContext:
        """Return trusted current time in the common default timezone."""

        return self.at(self._utc_now(), self._default_timezone)

    def at(self, moment: datetime, timezone: str) -> TimeContext:
        normalized = validate_timezone(timezone)
        utc = self._as_utc(moment)
        return TimeContext(utc=utc, local=utc.astimezone(ZoneInfo(normalized)), timezone=normalized)

    def _utc_now(self) -> datetime:
        return self._as_utc(self._clock.now())

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("clock must return an aware datetime")
        return value.astimezone(UTC)
