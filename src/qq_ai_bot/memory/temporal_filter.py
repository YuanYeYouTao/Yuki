"""Shared half-open event-time restrictions for bounded memory candidate reads."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.sql.elements import ColumnElement

from qq_ai_bot.memory.enums import MemoryTemporalConstraint
from qq_ai_bot.memory.models import MemoryTemporalIntent
from qq_ai_bot.persistence.models import MemoryFactModel


def strict_time_conditions(temporal: MemoryTemporalIntent | None) -> list[ColumnElement[bool]]:
    if temporal is None or temporal.constraint is not MemoryTemporalConstraint.STRICT:
        return []
    conditions: list[ColumnElement[bool]] = [MemoryFactModel.valid_from.is_not(None)]
    if temporal.start_at is not None:
        conditions.append(MemoryFactModel.valid_from >= temporal.start_at)
    if temporal.end_at is not None:
        conditions.append(MemoryFactModel.valid_from < temporal.end_at)
    return conditions


def strict_time_sql(temporal: MemoryTemporalIntent | None) -> tuple[str, dict[str, datetime]]:
    if temporal is None or temporal.constraint is not MemoryTemporalConstraint.STRICT:
        return "", {}
    sql = " AND mf.valid_from IS NOT NULL"
    parameters: dict[str, datetime] = {}
    if temporal.start_at is not None:
        sql += " AND mf.valid_from >= :memory_time_start"
        parameters["memory_time_start"] = temporal.start_at.replace(tzinfo=None)
    if temporal.end_at is not None:
        sql += " AND mf.valid_from < :memory_time_end"
        parameters["memory_time_end"] = temporal.end_at.replace(tzinfo=None)
    return sql, parameters
