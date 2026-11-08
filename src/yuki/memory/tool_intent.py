"""One strict, transport-neutral parser for model-issued memory read intent."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from yuki.memory.enums import (
    MemoryContextMode,
    MemoryTemporalConstraint,
    MemoryTemporalIntentMode,
)
from yuki.memory.models import MemoryQueryIntent, MemoryTemporalIntent


def parse_memory_tool_intent(arguments: dict[str, Any]) -> MemoryQueryIntent:
    """``since``/``until`` are timezone-qualified ISO-8601; until is exclusive and strict."""
    query = arguments.get("query")
    if query is not None and not isinstance(query, str):
        raise ValueError("query 必须是字符串")
    mode = MemoryContextMode.HYBRID if query and query.strip() else MemoryContextMode.OVERVIEW
    boundaries: list[datetime | None] = []
    for name in ("since", "until"):
        raw = arguments.get(name)
        if raw is None:
            boundaries.append(None)
            continue
        message = f"{name} 必须是带时区的 ISO-8601 时间，例如 2026-11-07T00:00:00+08:00"
        if not isinstance(raw, str) or not raw:
            raise ValueError(message)
        try:
            value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(message) from exc
        if value.utcoffset() is None:
            raise ValueError(message)
        boundaries.append(value)
    start, end = boundaries
    if start is not None and end is not None and start >= end:
        raise ValueError("since 必须早于 until")
    temporal = MemoryTemporalIntent()
    if start is not None or end is not None:
        temporal = MemoryTemporalIntent(
            mode=MemoryTemporalIntentMode.RANGE,
            constraint=MemoryTemporalConstraint.STRICT,
            start_at=start,
            end_at=end,
        )
    return MemoryQueryIntent(mode=mode, temporal=temporal)
