"""Trusted clock access and persistent per-person timezone preferences."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.models import PersonTimeSettingModel
from qq_ai_bot.services.canonical_owners import resolve_live_person_id
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
    if not normalized or len(normalized) > 64:
        raise ValueError("时区名称不能为空且不能超过 64 个字符")
    try:
        ZoneInfo(normalized)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"无效的 IANA 时区：{normalized}") from exc
    return normalized


class TimeContextService:
    """Resolve trusted current time using a persistent user timezone."""

    def __init__(
        self,
        database: Database,
        *,
        default_timezone: str = "Asia/Shanghai",
        clock: Clock | None = None,
    ) -> None:
        self._database = database
        self._default_timezone = validate_timezone(default_timezone)
        self._clock = clock or SystemClock()

    @property
    def clock(self) -> Clock:
        return self._clock

    async def timezone_for(self, user_id: str) -> str:
        async with self._database.sessions() as session:
            person_id = await resolve_live_person_id(session, user_id)
            row = await session.get(PersonTimeSettingModel, person_id)
        return row.timezone if row is not None else self._default_timezone

    async def set_timezone(self, user_id: str, timezone: str) -> str:
        normalized = validate_timezone(timezone)
        now = self._utc_now()
        async with self._database.sessions() as session, session.begin():
            person_id = await resolve_live_person_id(session, user_id)
            row = await session.get(PersonTimeSettingModel, person_id)
            if row is None:
                session.add(
                    PersonTimeSettingModel(
                        canonical_person_id=person_id,
                        timezone=normalized,
                        created_at=now,
                        updated_at=now,
                    )
                )
            else:
                row.timezone = normalized
                row.updated_at = now
        return normalized

    async def current(self, user_id: str) -> TimeContext:
        return self.at(self._utc_now(), await self.timezone_for(user_id))

    def current_default(self) -> TimeContext:
        """Return trusted current time without attributing a Person preference."""

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
