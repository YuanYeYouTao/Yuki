"""Bounded, byte-deterministic cards for external-agent wake facts.

A card is the only external-agent text a Work's model reads as an input: no native
event JSON, no progress. Fields are fixed, keys sorted and nulls dropped, so the
same facts always encode to the same bytes (prefix-cache rule, taskbook §5.1).
Free text is cut to the unified tool-result display budget (`tool-results.md`);
the full original stays readable by its ID.
"""

from __future__ import annotations

import json
from typing import Any

from yuki.config import Settings
from yuki.tool_results.json_length import json_string_prefix

# The same default display budget every tool result uses; not a card constant.
RESULT_BUDGET: int = Settings.model_fields["agent_tool_result_max_characters"].default

_REQUEST_EVENTS = frozenset({"permission", "question"})
_SUMMARY_KEYS = ("text", "result", "message", "error")


def wake_event(kind: str, payload: dict[str, Any], state: dict[str, Any] | None) -> bool:
    """Whether a native fact needs a decision: a request, a terminal, or a lost executor.

    Everything else (text, tool calls, plans, dispositions, resolutions, queue
    receipts) is progress: it stays on the timeline and wakes nobody.
    """
    if kind in _REQUEST_EVENTS:
        return True
    if isinstance(state, dict) and state.get("pending") is False:
        return True
    return payload.get("status") == "unknown"


def _bounded(value: Any, budget: int, name: str, truncated: list[str]) -> Any:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True)
    if len(encoded) <= budget:
        return value
    truncated.append(name)
    text = value if isinstance(value, str) else encoded
    return text[: json_string_prefix(text, budget)]


def encode_card(card: dict[str, Any]) -> str:
    return json.dumps(
        {key: value for key, value in card.items() if value is not None},
        ensure_ascii=False,
        sort_keys=True,
        allow_nan=False,
    )


def external_agent_card(
    *,
    event: str,
    operation_id: str | None,
    run_id: str | None,
    relationship: str,
    status: str | None = None,
    payload: dict[str, Any] | None = None,
    permission_id: str | None = None,
    session_id: str | None = None,
    execution_id: str | None = None,
    device_id: str | None = None,
    read: dict[str, Any] | None = None,
    budget: int = RESULT_BUDGET,
) -> str:
    payload = payload or {}
    truncated: list[str] = []
    pending = None
    if event in _REQUEST_EVENTS:
        tool = payload.get("tool")
        pending = {
            "type": event,
            "tool": _bounded(tool, budget, "tool", truncated) if tool is not None else None,
            "question": _bounded(payload["question"], budget, "question", truncated)
            if "question" in payload
            else None,
            "options": _bounded(payload["options"], budget, "options", truncated)
            if "options" in payload
            else None,
            "request": _bounded(payload["native_request"], budget, "request", truncated)
            if "native_request" in payload
            else None,
            # An observed session's requests belong to the user at the native terminal.
            "answer_at": "native_terminal" if relationship == "observe" else "yuki",
        }
        pending = {key: value for key, value in pending.items() if value is not None}
    summary = next(
        (payload[key] for key in _SUMMARY_KEYS if isinstance(payload.get(key), str)), None
    )
    return encode_card(
        {
            "kind": "external_agent_card",
            "event": event,
            "relationship": relationship,
            "operation_id": operation_id,
            "run_id": run_id,
            "session_id": session_id,
            "execution_id": execution_id,
            "device_id": device_id,
            "status": status,
            "reason": payload.get("reason") if isinstance(payload.get("reason"), str) else None,
            # Only an owner may answer; an observed request is never answerable here.
            "permission_id": permission_id if relationship != "observe" else None,
            "pending": pending,
            "summary": _bounded(summary, budget, "summary", truncated)
            if summary is not None
            else None,
            "truncated": sorted(truncated) or None,
            "read": read,
        }
    )
