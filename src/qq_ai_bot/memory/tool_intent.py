"""One strict, transport-neutral parser for model-issued memory read intent."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from qq_ai_bot.memory.enums import (
    MemoryContextMode,
    MemoryKind,
    MemoryRecallPurpose,
    MemoryTemporalConstraint,
    MemoryTemporalIntentMode,
)
from qq_ai_bot.memory.models import MemoryQueryIntent, MemoryTemporalIntent


def parse_memory_tool_intent(arguments: dict[str, Any]) -> MemoryQueryIntent:
    query = arguments.get("query")
    if query is not None and (not isinstance(query, str) or len(query) > 400):
        raise ValueError("invalid memory query")
    raw_mode = arguments.get("mode")
    if raw_mode is None:
        mode = MemoryContextMode.HYBRID if query and query.strip() else MemoryContextMode.OVERVIEW
    elif raw_mode == "relevant":
        mode = MemoryContextMode.HYBRID
    else:
        mode = MemoryContextMode(raw_mode)
        if mode is MemoryContextMode.NONE:
            raise ValueError("none is not a tool read mode")
    purpose = MemoryRecallPurpose(arguments.get("purpose", "recall"))
    entities = arguments.get("entities", [])
    if (
        not isinstance(entities, list)
        or len(entities) > 5
        or any(not isinstance(item, str) or not item.strip() or len(item) > 64 for item in entities)
    ):
        raise ValueError("invalid memory entities")
    kinds = arguments.get("preferred_kinds", [])
    if not isinstance(kinds, list) or len(kinds) > 3:
        raise ValueError("invalid memory kinds")
    preferred_kinds = tuple(MemoryKind(item) for item in kinds)
    constraint = MemoryTemporalConstraint(arguments.get("temporal_constraint", "strict"))
    boundaries: list[datetime | None] = []
    for name in ("start_at", "end_at"):
        raw = arguments.get(name)
        if raw is None:
            boundaries.append(None)
            continue
        if not isinstance(raw, str) or not raw:
            raise ValueError("invalid memory time")
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if constraint is MemoryTemporalConstraint.STRICT and value.utcoffset() is None:
            raise ValueError("strict memory time requires timezone")
        boundaries.append(value)
    start, end = boundaries
    temporal = MemoryTemporalIntent()
    if start is not None or end is not None:
        temporal = MemoryTemporalIntent(
            mode=MemoryTemporalIntentMode.RANGE,
            constraint=constraint,
            start_at=start,
            end_at=end,
        )
        if temporal.start_at is not None and temporal.end_at is not None:
            if temporal.start_at >= temporal.end_at:
                raise ValueError("memory range must have positive duration")
    return MemoryQueryIntent(
        mode=mode,
        purpose=purpose,
        entities=tuple(entities),
        preferred_kinds=preferred_kinds,
        temporal=temporal,
    )


def effective_query_summary(intent: MemoryQueryIntent) -> dict[str, object]:
    temporal = intent.temporal
    return {
        "mode": intent.mode.value,
        "purpose": intent.purpose.value,
        "start_at": temporal.start_at.isoformat() if temporal.start_at is not None else None,
        "end_at": temporal.end_at.isoformat() if temporal.end_at is not None else None,
        "temporal_constraint": temporal.constraint.value
        if temporal.mode is MemoryTemporalIntentMode.RANGE
        else None,
        "interval": "start_inclusive_end_exclusive",
    }
