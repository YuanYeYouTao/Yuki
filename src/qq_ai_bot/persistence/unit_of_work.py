"""Optional-session unit of work. Callers own commit when a session is provided."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from qq_ai_bot.persistence.database import Database

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def next_updated_at(previous: datetime | None, now: datetime | None = None) -> datetime:
    """Return a timestamp strictly after ``previous`` when the clock does not move."""

    current = _aware(now) if now is not None else datetime.now(UTC)
    if previous is None:
        return current
    prior = _aware(previous)
    return current if current > prior else prior + timedelta(microseconds=1)


def state_revision(updated_at: datetime) -> int:
    """Lossless microsecond token. Distinct ``updated_at`` values never collide."""

    stamp = _aware(updated_at).astimezone(UTC)
    delta = stamp - _EPOCH
    return max(1, delta.days * 86_400_000_000 + delta.seconds * 1_000_000 + delta.microseconds)


@asynccontextmanager
async def optional_session(
    database: Database,
    session: AsyncSession | None,
    *,
    write: bool,
) -> AsyncIterator[AsyncSession]:
    """Yield the caller session, or open a short-lived one.

    A provided session is never begun or committed here. Owned write
    sessions open ``begin()`` so existing callers keep their transaction.
    """

    if session is not None:
        yield session
        return
    async with database.sessions() as owned:
        if write:
            async with owned.begin():
                yield owned
            return
        yield owned
