"""Content versions used by observation and the Host admission compare-and-set."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from sqlalchemy import String, case, func, select, type_coerce
from sqlalchemy.sql.elements import ColumnElement
from sqlalchemy.sql.selectable import ScalarSelect

from yuki.domain.messages import MENTION_SNAPSHOT_FIELDS, mention_snapshots
from yuki.persistence.models import ChatEventModel, MemoryFactModel
from yuki.persistence.repository_records import EventRecord
from yuki.time.formatting import utc_iso

EVENT_TEXT_REVISION_FIELDS = ("content", "visual_summary", "audio_transcript")
EVENT_IDENTITY_REVISION_FIELDS = (
    "author_person_id",
    "author_kind",
    "suppression_status",
    "canonical_conversation_id",
    "caused_by_event_id",
    "reply_to_event_id",
)
MEMORY_TIME_REVISION_FIELDS = ("valid_from", "valid_until")
MEMORY_REVISION_FIELDS = (
    "content",
    "status",
    "review_state",
    "authority",
    "scope_type",
    "visibility_type",
    "canonical_subject_person_id",
    "canonical_visibility_person_id",
    "canonical_subject_space_id",
    "canonical_visibility_space_id",
    *MEMORY_TIME_REVISION_FIELDS,
)


def memory_revision_time_columns() -> tuple[ColumnElement[str], ColumnElement[str]]:
    """Read original SQLite date values without ORM datetime re-encoding."""
    return (
        type_coerce(MemoryFactModel.valid_from, String).label("revision_valid_from"),
        type_coerce(MemoryFactModel.valid_until, String).label("revision_valid_until"),
    )


def source_revision(row: Any) -> int:
    payload = [
        *(getattr(row, name) or "" for name in EVENT_TEXT_REVISION_FIELDS),
        *(getattr(row, name) for name in EVENT_IDENTITY_REVISION_FIELDS),
    ]
    segments = (
        row.segments if isinstance(row, EventRecord) else tuple(json.loads(row.segments_json))
    )
    mentions = mention_snapshots(segments)
    if mentions:
        payload.append(mentions)
    digest = hashlib.sha256(json.dumps(payload, ensure_ascii=False).encode()).hexdigest()
    return int(digest[:15], 16) + 1


def event_mentions_expression() -> ScalarSelect[str]:
    """SQLite projection of exactly the event-time mentions in source_revision."""
    parts = func.json_each(ChatEventModel.segments_json).table_valued("value")
    return (
        select(
            func.json_group_array(
                func.json_array(
                    *(
                        value
                        for key in MENTION_SNAPSHOT_FIELDS
                        for value in (
                            func.coalesce(func.json_type(parts.c.value, f"$.data.{key}"), "null"),
                            case(
                                (
                                    func.json_type(parts.c.value, f"$.data.{key}") == "text",
                                    func.json_extract(parts.c.value, f"$.data.{key}"),
                                ),
                                else_=func.printf(
                                    "%s",
                                    func.coalesce(parts.c.value.op("->")(f"$.data.{key}"), "null"),
                                ),
                            ),
                        )
                    )
                )
            )
        )
        .select_from(parts)
        .where(
            func.json_extract(parts.c.value, "$.type") == "mention",
            func.json_type(parts.c.value, "$.data") == "object",
        )
        .correlate(ChatEventModel)
        .scalar_subquery()
    )


def memory_revision(fact: Any) -> str:
    def value(item: Any) -> Any:
        if isinstance(item, datetime):
            return utc_iso(item)
        return getattr(item, "value", item)

    payload = [value(getattr(fact, name)) for name in MEMORY_REVISION_FIELDS]
    return hashlib.sha256(json.dumps(payload, default=str, ensure_ascii=False).encode()).hexdigest()
