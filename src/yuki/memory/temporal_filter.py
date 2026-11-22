"""Shared half-open event-time restrictions for bounded memory candidate reads."""

from __future__ import annotations

from sqlalchemy.sql.elements import ColumnElement

from yuki.memory.enums import MemoryTemporalConstraint
from yuki.memory.models import MemoryTemporalIntent
from yuki.persistence.models import MemoryFactModel


def strict_time_conditions(temporal: MemoryTemporalIntent | None) -> list[ColumnElement[bool]]:
    if temporal is None or temporal.constraint is not MemoryTemporalConstraint.STRICT:
        return []
    conditions: list[ColumnElement[bool]] = [MemoryFactModel.valid_from.is_not(None)]
    if temporal.start_at is not None:
        conditions.append(MemoryFactModel.valid_from >= temporal.start_at)
    if temporal.end_at is not None:
        conditions.append(MemoryFactModel.valid_from < temporal.end_at)
    return conditions
