"""Shared transactional helpers for repository implementations."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.domain.relationships import (
    RelationshipSnapshot,
    effective_trust,
    relationship_weight,
    stage_for_score,
)
from qq_ai_bot.identity.canonical_repository import (
    AccountRole,
    ensure_person,
    ensure_presence,
    ensure_space,
    require_person_binding,
)
from qq_ai_bot.persistence.models import (
    ChatEventModel,
    PersonRelationshipModel,
    RelationshipEventModel,
)
from qq_ai_bot.persistence.repository_records import (
    EventRecord,
    RelationshipEventRecord,
)

CANONICAL_KEEPER_STATUS = "keeper"


def suppression_is_canonical_live(status: str | None) -> bool:
    """True only for legacy-null or explicit keeper. Unknown nonempty fails closed."""

    return status is None or status == CANONICAL_KEEPER_STATUS


def keeper_event_clause() -> ColumnElement[bool]:
    """SQLAlchemy live-event filter shared by canonical history, rollup, and Memory."""

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


async def _ensure_person(
    session: AsyncSession,
    user_id: str,
    *,
    nickname: str = "",
    is_bot: bool = False,
    now: datetime | None = None,
    canonical_role: AccountRole | None = None,
) -> str | None:
    """Ensure a canonical account owner without creating a legacy carrier."""

    timestamp = now or datetime.now(UTC)
    role = canonical_role or ("external_bot" if is_bot else "human")
    if role == "yuki_self":
        return await ensure_presence(session, user_id, now=timestamp)
    if role == "external_bot":
        return None
    return await ensure_person(session, user_id, display_name=nickname, now=timestamp)


async def _ensure_relationship(
    session: AsyncSession,
    user_id: str,
    *,
    initial_affection: int = 50,
    initial_trust: int = 50,
    now: datetime | None = None,
) -> PersonRelationshipModel:
    timestamp = now or datetime.now(UTC)
    binding = await require_person_binding(session, user_id)
    row = await session.get(PersonRelationshipModel, binding.person_id)
    if row is None:
        row = PersonRelationshipModel(
            canonical_person_id=binding.person_id,
            affection_score=initial_affection,
            trust_score=initial_trust,
            created_at=timestamp,
            updated_at=timestamp,
            last_automatic_change_at=None,
        )
        session.add(row)
        await session.flush()
    return row


async def _ensure_group(
    session: AsyncSession,
    group_id: str,
    *,
    name: str = "",
    enabled: bool | None = None,
    now: datetime | None = None,
) -> str:
    return await ensure_space(
        session,
        group_id,
        name=name,
        enabled=True if enabled is None else enabled,
        now=now or datetime.now(UTC),
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
    raw_mentions = context.get("mentioned_user_ids", ())
    mentioned_user_ids = (
        tuple(str(item) for item in raw_mentions if str(item))
        if isinstance(raw_mentions, list | tuple)
        else ()
    )
    raw_reply_sender = context.get("reply_sender_user_id")
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
        bot_user_id=str(_row_value(row, "bot_user_id")),
        platform_message_id=str(_row_value(row, "platform_message_id")),
        scope_type=scope_type if isinstance(scope_type, ScopeType) else ScopeType(str(scope_type)),
        sender_user_id=str(_row_value(row, "sender_user_id")),
        sender_nickname=str(_row_value(row, "sender_nickname") or ""),
        sender_group_card=str(_row_value(row, "sender_group_card") or ""),
        direction=str(_row_value(row, "direction")),
        content=str(_row_value(row, "content") or ""),
        visual_summary=str(_row_value(row, "visual_summary") or ""),
        segments=segments,
        occurred_at=occurred,
        group_id=_row_value(row, "group_id"),
        private_peer_user_id=_row_value(row, "private_peer_user_id"),
        reply_to_message_id=_row_value(row, "reply_to_message_id"),
        origin=str(_row_value(row, "origin") or "user_message"),
        automation_id=_row_value(row, "automation_id"),
        automation_run_id=_row_value(row, "automation_run_id"),
        mentioned_user_ids=mentioned_user_ids,
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
    )


def _relationship_snapshot(
    row: PersonRelationshipModel,
    *,
    user_id: str,
    trust_cap_offset: int,
) -> RelationshipSnapshot:
    usable_trust = effective_trust(
        row.affection_score,
        row.trust_score,
        cap_offset=trust_cap_offset,
    )
    return RelationshipSnapshot(
        user_id=user_id,
        affection_score=row.affection_score,
        trust_score=row.trust_score,
        effective_trust=usable_trust,
        relationship_weight=relationship_weight(row.affection_score, usable_trust),
        stage=stage_for_score(row.affection_score),
        updated_at=row.updated_at,
    )


def _relationship_event_record(
    row: RelationshipEventModel,
    *,
    user_id: str,
) -> RelationshipEventRecord:
    return RelationshipEventRecord(
        id=row.id,
        user_id=user_id,
        source_event_id=row.source_event_id,
        actor_user_id=row.actor_user_id,
        change_type=row.change_type,
        affection_before=row.affection_before,
        affection_delta=row.affection_delta,
        affection_after=row.affection_after,
        trust_before=row.trust_before,
        trust_delta=row.trust_delta,
        trust_after=row.trust_after,
        reason_code=row.reason_code,
        confidence=row.confidence,
        created_at=row.created_at,
    )
