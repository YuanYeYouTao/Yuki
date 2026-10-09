"""Bounded derived continuity data; references never prove semantic correctness."""

from __future__ import annotations

import json
from typing import Any

SCHEMA = "conversation_rollup_v1"

SUMMARY_INSTRUCTION = (
    'Return a JSON object with "continuity": a concise narrative string and '
    '"source_event_ids": internal integer IDs, '
    '"open_issues": [{"text": string, "source_event_ids": [integers]}], '
    '"corrections": [{"text": string, "source_event_ids": [integers], '
    '"supersedes_event_ids": [integers]}]. Empty open_issues and corrections may be omitted. '
    "Cite only supplied sources or "
    "references carried in the previous summary. Never use platform IDs as references. "
    "Update resolved open issues instead of accumulating them. New corrections supersede "
    "older claims: rewrite continuity to reflect the correction, keep its source and any "
    "known superseded source IDs, and do not revive the obsolete claim as current fact. "
    "Prior legacy text may contain unverified claims; preserve necessary continuity but "
    "do not invent source IDs for it. Reference validity does not establish truth. "
)


def summary_response_format() -> dict[str, Any]:
    """Use the existing provider schema contract, with no synthetic result tool.

    Keep the wire schema basic across adapters. Reference membership remains
    a local check even when the provider constrains the JSON shape.
    """
    ids = {"type": "array", "items": {"type": "integer"}}
    properties = {"text": {"type": "string"}, "source_event_ids": ids}
    issue = {
        "type": "object",
        "properties": properties,
        "required": ["text"],
        "additionalProperties": True,
    }
    correction_properties = {**properties, "supersedes_event_ids": ids}
    correction = {
        "type": "object",
        "properties": correction_properties,
        "required": ["text"],
        "additionalProperties": True,
    }
    root_properties = {
        "schema": {"type": "string"},
        "continuity": {"type": "string"},
        "source_event_ids": ids,
        "open_issues": {"type": "array", "items": issue},
        "corrections": {"type": "array", "items": correction},
    }
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "conversation_rollup",
            "strict": False,
            "schema": {
                "type": "object",
                "properties": root_properties,
                "required": ["continuity"],
                "additionalProperties": True,
            },
        },
    }


def _ids(value: Any) -> set[int]:
    if not isinstance(value, list) or any(type(item) is not int or item < 1 for item in value):
        raise ValueError("rollup_summary_invalid_references")
    return set(value)


def parse_summary(text: str) -> dict[str, Any]:
    """Normalize consumed fields without guessing what their prose means."""
    try:
        value = json.loads(text)
    except (ValueError, TypeError) as exc:
        raise ValueError("rollup_summary_invalid_json") from exc
    if not isinstance(value, dict):
        raise ValueError("rollup_summary_invalid_schema")
    if not isinstance(value.get("continuity"), str) or not value["continuity"].strip():
        raise ValueError("rollup_summary_empty_continuity")
    result: dict[str, Any] = {
        "schema": SCHEMA,
        "continuity": value["continuity"],
        "source_event_ids": sorted(_ids(value.get("source_event_ids", []))),
        "open_issues": [],
        "corrections": [],
    }
    for name in ("open_issues", "corrections"):
        entries = value.get(name, [])
        if not isinstance(entries, list):
            raise ValueError("rollup_summary_invalid_items")
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValueError("rollup_summary_invalid_item")
            body = entry.get("text")
            if not isinstance(body, str) or not body.strip():
                raise ValueError("rollup_summary_invalid_item_text")
            normalized: dict[str, Any] = {
                "text": body,
                "source_event_ids": sorted(_ids(entry.get("source_event_ids", []))),
            }
            if name == "corrections":
                normalized["supersedes_event_ids"] = sorted(
                    _ids(entry.get("supersedes_event_ids", []))
                )
            result[name].append(normalized)
    return result


def summary_references(value: dict[str, Any]) -> set[int]:
    references = set(value["source_event_ids"])
    for entry in (*value["open_issues"], *value["corrections"]):
        references.update(entry["source_event_ids"])
        references.update(entry.get("supersedes_event_ids", ()))
    return references


def previous_summary_input(text: str) -> str:
    """Legacy checkpoints remain readable; only new model output is structured."""
    if not text.strip():
        return "(none)"
    try:
        parse_summary(text)
    except ValueError:
        return "[Legacy narrative; source references unverified]\n" + text.strip()
    return text.strip()
