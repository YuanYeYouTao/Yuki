"""UTC schedule calculations with explicit IANA timezone behavior."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from yuki.automation.models import (
    AfterSchedule,
    DailySchedule,
    IntervalSchedule,
    OnceSchedule,
    Schedule,
)
from yuki.time.service import validate_timezone


def initial_run_at(
    schedule: Schedule, now_utc: datetime, script_timezone: str = "Asia/Shanghai"
) -> datetime:
    """Calculate the first unique UTC execution instant at creation time."""

    now = _as_utc(now_utc)
    if isinstance(schedule, AfterSchedule):
        return now + timedelta(seconds=schedule.seconds)
    if isinstance(schedule, IntervalSchedule):
        return now + timedelta(seconds=schedule.seconds)
    if isinstance(schedule, OnceSchedule):
        candidate = schedule.local_datetime.astimezone(UTC)
        if candidate <= now:
            raise ValueError("once 的执行时间必须晚于当前可信时间")
        return candidate
    return next_run_at(schedule, now, script_timezone)


def next_run_at(
    schedule: Schedule, after_utc: datetime, script_timezone: str = "Asia/Shanghai"
) -> datetime:
    """Return the first scheduled instant strictly after ``after_utc``."""

    after = _as_utc(after_utc)
    if isinstance(schedule, AfterSchedule | OnceSchedule):
        raise ValueError("一次性 schedule 没有下一次执行时间")
    if isinstance(schedule, IntervalSchedule):
        return after + timedelta(seconds=schedule.seconds)
    zone_name = validate_timezone(schedule.timezone or script_timezone)
    zone = ZoneInfo(zone_name)
    local_after = after.astimezone(zone)
    day = local_after.date()
    while True:
        if isinstance(schedule, DailySchedule):
            candidate = _resolve_wall_time(
                day,
                schedule.hour,
                schedule.minute,
                zone_name,
            )
            if candidate > after:
                return candidate
        elif day.isoweekday() in schedule.weekdays:
            candidate = _resolve_wall_time(day, schedule.hour, schedule.minute, zone_name)
            if candidate > after:
                return candidate
        day += timedelta(days=1)


def schedule_after_completion(
    schedule: Schedule,
    scheduled_for: datetime,
    now_utc: datetime,
    script_timezone: str = "Asia/Shanghai",
) -> datetime | None:
    """Advance periodic schedules directly to one future slot without catch-up storms."""

    if isinstance(schedule, AfterSchedule | OnceSchedule):
        return None
    cursor = max(_as_utc(scheduled_for), _as_utc(now_utc))
    return next_run_at(schedule, cursor, script_timezone)


def _resolve_local(value: datetime, timezone: str) -> datetime:
    timezone = validate_timezone(timezone)
    if value.tzinfo is not None:
        return value.astimezone(UTC)
    aware = value.replace(tzinfo=ZoneInfo(timezone), fold=0)
    return aware.astimezone(UTC)


def _resolve_wall_time(
    day: date,
    hour: int,
    minute: int,
    timezone: str,
) -> datetime:
    """Resolve ambiguous time once and move nonexistent wall times forward."""

    return _resolve_local(datetime.combine(day, time(hour=hour, minute=minute)), timezone)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("schedule timestamps must be timezone-aware")
    return value.astimezone(UTC)
