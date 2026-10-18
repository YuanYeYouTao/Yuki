"""Shared transactional helpers for repository implementations."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from sqlalchemy import or_
from sqlalchemy.sql.elements import ColumnElement

from yuki.domain.conversations import ScopeType
from yuki.domain.messages import MessageMention
from yuki.persistence.models import ChatEventModel
from yuki.persistence.repository_records import EventRecord

CANONICAL_KEEPER_STATUS = "keeper"


def suppression_is_canonical_live(status: str | None) -> bool:
    """True only for legacy-null or explicit keeper. Unknown nonempty fails closed."""

    return status is None or status == CANONICAL_KEEPER_STATUS


def keeper_event_clause() -> ColumnElement[bool]:
    """SQLAlchemy live-event filter shared by canonical history and Memory."""

    return or_(
        ChatEventModel.suppression_status.is_(None),
        ChatEventModel.suppression_status == CANONICAL_KEEPER_STATUS,
    )


def sql_keeper_event_predicate(alias: str = "c") -> str:
    """Raw-SQL form of ``keeper_event_clause`` for hygiene/audit text queries."""

    return (
        f"({alias}.suppression_status IS NULL OR "
        f"{alias}.suppression_status='{CANONICAL_KEEPER_STATUS}')"
    )


def _row_value(row: ChatEventModel | Mapping[str, Any], name: str) -> Any:
    if isinstance(row, Mapping):
        return row.get(name)
    return getattr(row, name)


def _event_record(row: ChatEventModel | Mapping[str, Any]) -> EventRecord:
    raw_segments = _row_value(row, "segments_json")
    try:
        decoded = json.loads(raw_segments) if isinstance(raw_segments, str) else raw_segments
    except json.JSONDecodeError:
        decoded = []
    if not isinstance(decoded, list):
        decoded = []
    segments = tuple(item for item in decoded if isinstance(item, dict))
    context: dict[str, object] = next(
        (
            item.get("data", {})
            for item in reversed(segments)
            if item.get("type") == "yuki_context" and isinstance(item.get("data"), dict)
        ),
        {},
    )
    projected_content = None
    projected_mentions: tuple[MessageMention, ...] = ()
    legacy_mentions: tuple[str, ...] = ()
    if context.get("platform") in (None, "", "qq"):
        # Genuine old QQ AT segments predate the stored mention snapshot.
        from yuki.adapters.onebot.legacy_event import project_legacy_segments

        projection = project_legacy_segments(segments, str(_row_value(row, "bot_user_id")))
        if projection is not None:
            projected_content = projection.text
            projected_mentions = projection.ordered_mentions
            legacy_mentions = projection.mentioned_user_ids
    raw_mentions = context.get("mentioned_user_ids", legacy_mentions)
    mentioned_user_ids = (
        tuple(str(item) for item in raw_mentions if str(item))
        if isinstance(raw_mentions, list | tuple)
        else ()
    )
    raw_reply_sender = context.get("reply_sender_user_id")
    raw_people = context.get("mentioned_person_ids", ())
    mentioned_person_ids = (
        tuple(str(item) for item in raw_people) if isinstance(raw_people, list | tuple) else ()
    )
    external_payload: dict[str, object] | None = None
    raw_external = _row_value(row, "external_payload_json")
    if raw_external:
        try:
            raw_payload = (
                json.loads(raw_external) if isinstance(raw_external, str) else raw_external
            )
        except json.JSONDecodeError:
            raw_payload = None
        if isinstance(raw_payload, dict):
            external_payload = raw_payload
    occurred = _row_value(row, "occurred_at")
    if isinstance(occurred, str):
        occurred = datetime.fromisoformat(occurred)
    scope_type = _row_value(row, "scope_type")
    return EventRecord(
        id=int(_row_value(row, "id")),
        projected_content=projected_content,
        projected_mentions=projected_mentions,
        platform=str(context.get("platform") or ""),
        thread_id=str(context["thread_id"]) if context.get("thread_id") is not None else None,
        bot_user_id=str(_row_value(row, "bot_user_id")),
        platform_message_id=str(_row_value(row, "platform_message_id")),
        scope_type=scope_type if isinstance(scope_type, ScopeType) else ScopeType(str(scope_type)),
        sender_user_id=str(_row_value(row, "sender_user_id")),
        sender_nickname=str(_row_value(row, "sender_nickname") or ""),
        sender_group_card=str(_row_value(row, "sender_group_card") or ""),
        direction=str(_row_value(row, "direction")),
        content=str(_row_value(row, "content") or ""),
        visual_summary=str(_row_value(row, "visual_summary") or ""),
        audio_transcript=str(_row_value(row, "audio_transcript") or ""),
        segments=segments,
        occurred_at=occurred,
        group_id=_row_value(row, "group_id"),
        private_peer_user_id=_row_value(row, "private_peer_user_id"),
        reply_to_message_id=_row_value(row, "reply_to_message_id"),
        reply_to_event_id=_row_value(row, "reply_to_event_id"),
        origin=str(_row_value(row, "origin") or "user_message"),
        automation_id=_row_value(row, "automation_id"),
        automation_run_id=_row_value(row, "automation_run_id"),
        mentioned_user_ids=mentioned_user_ids,
        mentioned_person_ids=mentioned_person_ids,
        reply_sender_user_id=str(raw_reply_sender) if raw_reply_sender else None,
        event_kind=str(_row_value(row, "event_kind") or "message"),
        source_plugin_id=_row_value(row, "source_plugin_id"),
        external_source=_row_value(row, "external_source"),
        external_event_key=_row_value(row, "external_event_key"),
        external_event_type=_row_value(row, "external_event_type"),
        external_payload=external_payload,
        canonical_conversation_id=_row_value(row, "canonical_conversation_id"),
        canonical_event_id=_row_value(row, "canonical_event_id"),
        author_kind=_row_value(row, "author_kind"),
        author_person_id=_row_value(row, "author_person_id"),
        author_presence_id=_row_value(row, "author_presence_id"),
        ingress_presence_id=_row_value(row, "ingress_presence_id"),
        suppression_status=_row_value(row, "suppression_status"),
        caused_by_event_id=_row_value(row, "caused_by_event_id"),
    )
