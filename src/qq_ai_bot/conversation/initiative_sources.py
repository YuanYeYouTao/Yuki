"""Content versions used by observation and the Host admission compare-and-set."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any


def source_revision(row: Any) -> int:
    payload = [
        row.content or "",
        row.visual_summary or "",
        row.audio_transcript or "",
        row.author_person_id,
        row.author_kind,
        row.suppression_status,
        row.canonical_conversation_id,
        row.caused_by_event_id,
        row.reply_to_event_id,
    ]
    digest = hashlib.sha256(json.dumps(payload, ensure_ascii=False).encode()).hexdigest()
    return int(digest[:15], 16) + 1


def memory_revision(fact: Any) -> str:
    def value(item: Any) -> Any:
        if isinstance(item, datetime):
            return (
                item.replace(tzinfo=UTC) if item.tzinfo is None else item.astimezone(UTC)
            ).isoformat()
        return getattr(item, "value", item)

    payload = [
        fact.content,
        value(fact.status),
        value(fact.review_state),
        value(fact.authority),
        value(fact.scope_type),
        value(fact.visibility_type),
        fact.canonical_subject_person_id,
        fact.canonical_visibility_person_id,
        fact.canonical_subject_space_id,
        fact.canonical_visibility_space_id,
        value(fact.valid_from),
        value(fact.valid_until),
    ]
    return hashlib.sha256(json.dumps(payload, default=str, ensure_ascii=False).encode()).hexdigest()
