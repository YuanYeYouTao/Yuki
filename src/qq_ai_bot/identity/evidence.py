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


def fact_conversation_aligns(
    *,
    scope_type: str,
    visibility_type: str | None,
    subject_person_id: str | None,
    subject_space_id: str | None,
    visibility_person_id: str | None,
    visibility_space_id: str | None,
    conversation_person_id: str | None,
    conversation_space_id: str | None,
) -> bool:
    """Whether one canonical Conversation can provide evidence for a fact."""

    scope = str(scope_type or "")
    subject_person = subject_person_id or None
    subject_space = subject_space_id or None
    visibility_person = visibility_person_id or None
    visibility_space = visibility_space_id or None
    conv_person = conversation_person_id or None
    conv_space = conversation_space_id or None
    if bool(conv_person) == bool(conv_space):
        return False
    if scope == "person":
        if not subject_person or subject_space or visibility_person or visibility_space:
            return False
        return conv_person == subject_person if conv_person is not None else conv_space is not None
    if scope == "group":
        if not subject_space or subject_person or visibility_person or visibility_space:
            return False
        return conv_space == subject_space
    if scope == "person_group":
        if not subject_person or not subject_space or visibility_person or visibility_space:
            return False
        return conv_space == subject_space and (
            conv_person is None or conv_person == subject_person
        )
    if scope != "self":
        return False
    if visibility_type in {None, "global"}:
        return not any((subject_person, subject_space, visibility_person, visibility_space))
    if visibility_type == "private":
        if not visibility_person or any((subject_person, subject_space, visibility_space)):
            return False
        return conv_person == visibility_person
    if visibility_type == "group":
        if not visibility_space or any((subject_person, subject_space, visibility_person)):
            return False
        return conv_space == visibility_space
    return False


__all__ = [
    "event_is_canonical_live",
    "event_suppression_is_hidden",
    "fact_conversation_aligns",
]
