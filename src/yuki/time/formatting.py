"""Consistent user-facing formatting for trusted timestamps."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import overload
from zoneinfo import ZoneInfo


def stored_utc(value: datetime) -> datetime:
    """Normalize a persisted timestamp; naive database values are UTC."""

    aware = value.replace(tzinfo=UTC) if value.tzinfo is None else value
    return aware.astimezone(UTC)


def local_datetime(value: datetime, timezone: str) -> datetime:
    """Convert one persisted UTC timestamp to an explicit local datetime."""

    return stored_utc(value).astimezone(ZoneInfo(timezone))


@overload
def utc_iso(value: datetime) -> str: ...


@overload
def utc_iso(value: None) -> None: ...


def utc_iso(value: datetime | None) -> str | None:
    """Render a persisted timestamp with an explicit UTC offset."""

    return stored_utc(value).isoformat() if value is not None else None


@overload
def local_iso(value: datetime, timezone: str) -> str: ...


@overload
def local_iso(value: None, timezone: str) -> None: ...


def local_iso(value: datetime | None, timezone: str) -> str | None:
    """Render a stored UTC timestamp in an explicit IANA timezone."""

    if value is None:
        return None
    return local_datetime(value, timezone).isoformat()


def local_text(value: datetime | None, timezone: str) -> str:
    """Render a compact local timestamp suitable for QQ messages."""

    rendered = local_iso(value, timezone)
    if rendered is None:
        return "无"
    return datetime.fromisoformat(rendered).strftime("%Y-%m-%d %H:%M:%S")
