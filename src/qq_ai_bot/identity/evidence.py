"""Canonical event/evidence readability invariants."""

from __future__ import annotations

from qq_ai_bot.persistence.repository_helpers import suppression_is_canonical_live


def event_is_canonical_live(
    *,
    canonical_event_id: object,
    suppression_status: object,
    canonical_conversation_id: object,
    author_kind: object,
) -> bool:
    if not canonical_event_id or not canonical_conversation_id:
        return False
    if author_kind is None or not str(author_kind).strip():
        return False
    status = None if suppression_status is None else str(suppression_status)
    return suppression_is_canonical_live(status)


def event_suppression_is_hidden(suppression_status: object) -> bool:
    status = None if suppression_status is None else str(suppression_status)
    return not suppression_is_canonical_live(status)


__all__ = [
    "event_is_canonical_live",
    "event_suppression_is_hidden",
]
