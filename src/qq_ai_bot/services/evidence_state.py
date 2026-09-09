"""Additive tool-result evidence metadata, derived only from backend payloads."""

from typing import Any, Literal


def evidence_state(
    payload: dict[str, Any], source: Literal["memory_tool", "web_tool"]
) -> dict[str, Any]:
    data = payload.get("data")
    data = data if isinstance(data, dict) else {}
    rows = data.get("memories" if source == "memory_tool" else "sources", [])
    rows = rows if isinstance(rows, list) else []
    if source == "memory_tool" and isinstance(data.get("memory"), dict):
        rows = [data["memory"]]
    if source == "memory_tool" and isinstance(data.get("evidence"), list):
        rows = data["evidence"]
    key = "memory_ref" if source == "memory_tool" else "source_id"
    if key in data:
        rows = [data]
    refs = list(
        dict.fromkeys(
            row[key] for row in rows if isinstance(row, dict) and isinstance(row.get(key), str)
        )
    )
    if payload.get("ok") is True:
        status = "success" if rows else "empty"
    elif payload.get("error") in {"permission_denied", "url_not_authorized"}:
        status = "denied"
    else:
        status = "failed"
    return {
        "source": source,
        "query_status": status,
        "returned_count": len(rows),
        "truncated": data.get("truncated") is True,
        "partial_failure": data.get("partial_failure") is True,
        "source_refs": refs,
        "delivery": "staged",
    }
