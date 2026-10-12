"""Compact known Host context fields without changing their source records."""

from __future__ import annotations

from typing import Any


def project_short_state(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Present actual occupied slots without storage bookkeeping."""
    return [{"slot": row["slot"], "text": row["text"]} for row in rows if row["text"]]


def _person(data: dict[str, Any]) -> dict[str, Any]:
    projected = dict(data)
    for key in ("nickname", "display_name"):
        if projected.get(key) in (None, ""):
            projected.pop(key, None)
    if projected.get("nickname") == projected.get("display_name"):
        projected.pop("nickname", None)
    return projected


def project_people_and_scene(metadata: dict[str, Any]) -> dict[str, Any]:
    """Only project known identity items; facts, external events and plugin JSON stay intact."""
    items = metadata.get("items")
    if not isinstance(items, list) or not all(
        isinstance(item, dict) and isinstance(item.get("id"), str) and "data" in item
        for item in items
    ):
        return metadata
    projected_items: list[dict[str, Any]] = []
    for item in items:
        item_id, data = item.get("id"), item.get("data")
        if isinstance(data, dict):
            if item_id == "conversation_target_person":
                data = _person(data)
            elif item_id == "scene":
                data = dict(data)
                if data.get("type") == "private" and data.get("group_id") is None:
                    data.pop("group_id", None)
        projected_items.append({**item, "data": data})
    return {**metadata, "items": projected_items}
