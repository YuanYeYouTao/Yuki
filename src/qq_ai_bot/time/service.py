"""Trusted clock access and persistent per-person timezone preferences."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy.ext.asyncio import AsyncSession

from qq_ai_bot.identity.canonical_projections import resolve_canonical_time_setting
from qq_ai_bot.identity.db_models import CanonicalPersonModel, CanonicalSpaceModel, PresenceModel
from qq_ai_bot.identity.errors import CanonicalIdentityError
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.time.models import TimeContext

_CANONICAL_KIND_MISMATCH = "canonical_kind_mismatch"
_CANONICAL_OWNER_DISABLED = "canonical_owner_disabled"


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
            try:
                row = await resolve_canonical_time_setting(session, user_id)
            except CanonicalIdentityError:
                try:
                    fallback = await _timezone_for_canonical_uuid(
                        session, user_id, default_timezone=self._default_timezone
                    )
                except CanonicalIdentityError as mapped:
                    raise mapped from None
                if fallback is None:
                    raise
                return fallback
        return row.timezone if row is not None else self._default_timezone

    async def set_timezone(self, user_id: str, timezone: str) -> str:
        normalized = validate_timezone(timezone)
        now = self._utc_now()
        async with self._database.sessions() as session, session.begin():
            await resolve_canonical_time_setting(
                session,
                user_id,
                timezone=normalized,
                now=now,
            )
        return normalized

    async def current(self, user_id: str) -> TimeContext:
        return self.at(self._utc_now(), await self.timezone_for(user_id))

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


async def _timezone_for_canonical_uuid(
    session: AsyncSession, user_id: str, *, default_timezone: str
) -> str | None:
    """Person UUID fallback: enabled Person may use default; others fail closed.

    Returns None when user_id is not a Person/Presence/Space primary key so the
    original binding error can be re-raised.
    """

    if await session.get(PresenceModel, user_id) is not None:
        raise CanonicalIdentityError(_CANONICAL_KIND_MISMATCH)
    if await session.get(CanonicalSpaceModel, user_id) is not None:
        raise CanonicalIdentityError(_CANONICAL_KIND_MISMATCH)
    person = await session.get(CanonicalPersonModel, user_id)
    if person is None:
        return None
    if not person.enabled:
        raise CanonicalIdentityError(_CANONICAL_OWNER_DISABLED)
    return default_timezone
