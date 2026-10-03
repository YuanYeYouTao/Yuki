"""Compact known Host context fields without changing their source records."""

from __future__ import annotations

from typing import Any


def project_short_state(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Empty slots still expose the revision needed by update_short_state CAS."""
    return [
        {key: value for key, value in row.items() if key not in {"text", "expires_at"}}
        if row.get("text") == ""
        else dict(row)
        for row in rows
    ]


def project_recent_delivery(rows: tuple[dict[str, object], ...]) -> list[dict[str, object]]:
    return [
        {key: value for key, value in row.items() if key != "media_kinds" or value != []}
        for row in rows
    ]


def _person(data: dict[str, Any]) -> dict[str, Any]:
    projected = dict(data)
    for key in ("nickname", "display_name"):
        if projected.get(key) in (None, ""):
            projected.pop(key, None)
    if projected.get("nickname") == projected.get("display_name"):
        projected.pop("nickname", None)
    for key in ("facts", "person_facts", "group_facts", "aliases"):
        if projected.get(key) == []:
            projected.pop(key)
    return projected


def project_people_and_scene(metadata: dict[str, Any]) -> dict[str, Any]:
    """Only project known identity items; facts, external events and plugin JSON stay intact."""
    items = metadata.get("items")
    if not isinstance(items, list) or not all(
        isinstance(item, dict) and isinstance(item.get("id"), str) and "data" in item
        for item in items
    ):
        return metadata
    identities = {
        item["id"]: item["data"]
        for item in items
        if isinstance(item.get("id"), str) and isinstance(item.get("data"), dict)
    }
    current = identities.get("current_person", {})
    scene = identities.get("scene", {})
    aliases_seen = {
        prefix: {
            value
            for key in ("nickname", "display_name")
            if isinstance(value := identities.get(person_id, {}).get(key), str) and value
        }
        for prefix, person_id in (
            ("current_alias.", "current_person"),
            ("conversation_target_alias.", "conversation_target_person"),
        )
    }
    projected_items: list[dict[str, Any]] = []
    for item in items:
        item_id, data = item.get("id"), item.get("data")
        if isinstance(item_id, str) and isinstance(data, str):
            prefix = next((key for key in aliases_seen if item_id.startswith(key)), None)
            if prefix is not None:
                if not data or data in aliases_seen[prefix]:
                    continue
                aliases_seen[prefix].add(data)
        if isinstance(data, dict):
            if item_id in {"current_person", "conversation_target_person"} or (
                isinstance(item_id, str) and item_id.startswith("referenced_person.")
            ):
                data = _person(data)
            elif item_id == "scene":
                data = dict(data)
                card = data.get("group_card")
                if card in (None, "") or (
                    isinstance(card, str)
                    and card in (current.get("nickname"), current.get("display_name"))
                ):
                    data.pop("group_card", None)
                if data.get("type") == "private" and data.get("group_id") is None:
                    data.pop("group_id", None)
            elif item_id in {"current_group", "current_person_in_group"}:
                data = dict(data)
                for key, identity in (("group_id", scene), ("user_id", current)):
                    if (
                        isinstance(identity.get(key), str)
                        and identity[key]
                        and (data.get(key) == identity[key])
                    ):
                        data.pop(key, None)
                if data.get("facts") == []:
                    data.pop("facts")
                if not data:
                    continue
        projected_items.append({**item, "data": data})
    return {**metadata, "items": projected_items}
