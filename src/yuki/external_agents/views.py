"""Model view of native-agent reads: internal receipt bookkeeping stays in typed data."""

from __future__ import annotations

from typing import Any

# Receipt bookkeeping kept in typed data for recovery, never shown to the model.
_AGENT_INTERNAL = frozenset(
    {
        "kind",
        "request_id",
        "admitted",
        "native_reference",
        "ownership",
        "cursor_parameter",
        "query_scope",
        "status_scope",
        "retryable",
        "uncertain",
        "error",
        "last_confirmed",
    }
)


def agent_view(data: dict[str, Any]) -> dict[str, Any]:
    view = {
        key: value
        for key, value in data.items()
        if key not in _AGENT_INTERNAL and value is not None
    }
    cursor = data.get("next_cursor")
    if isinstance(cursor, int) and not isinstance(cursor, bool):
        view["next_cursor"] = str(cursor)
    return view
