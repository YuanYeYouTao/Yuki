"""Shared transactional helpers for repository implementations."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.ext.asyncio import AsyncSession

from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.domain.relationships import (
    RelationshipSnapshot,
    effective_trust,
    relationship_weight,
    stage_for_score,
)
from qq_ai_bot.identity.dual_write import (
    AccountRole,
    require_v1_runtime,
    sync_account,
    sync_space,
)
from qq_ai_bot.identity.errors import IdentityDualWriteError
from qq_ai_bot.persistence.models import (
    ChatEventModel,
    GroupModel,
    PersonModel,
    PersonRelationshipModel,
    RelationshipEventModel,
)
from qq_ai_bot.persistence.repository_records import (
    EventRecord,
    RelationshipEventRecord,
)


async def _ensure_person(
    session: AsyncSession,
    user_id: str,
    *,
    nickname: str = "",
    is_bot: bool = False,
    now: datetime | None = None,
    canonical_role: AccountRole | None = None,
) -> PersonModel:
    timestamp = now or datetime.now(UTC)
    await require_v1_runtime(session)
    person = await session.get(PersonModel, user_id)
    if person is None:
        await session.execute(
            insert(PersonModel)
            .values(
                user_id=user_id,
                nickname=nickname,
                enabled=True,
                is_bot=is_bot,
                first_seen_at=timestamp,
                last_seen_at=timestamp,
            )
            .on_conflict_do_nothing(index_elements=["user_id"])
        )
        person = await session.get(PersonModel, user_id)
        if person is None:
            raise IdentityDualWriteError("unclassified")
    if nickname:
        person.nickname = nickname
    person.is_bot = person.is_bot or is_bot
    person.last_seen_at = timestamp
    await sync_account(
        session,
        user_id,
        role=canonical_role,
        is_bot=person.is_bot,
        display_name=person.nickname,
        now=timestamp,
    )
    return person


async def _ensure_relationship(
    session: AsyncSession,
    user_id: str,
    *,
    initial_affection: int = 50,
    initial_trust: int = 50,
    now: datetime | None = None,
) -> PersonRelationshipModel:
    timestamp = now or datetime.now(UTC)
    row = await session.get(PersonRelationshipModel, user_id)
    if row is None:
        await session.execute(
            insert(PersonRelationshipModel)
            .values(
                user_id=user_id,
                affection_score=initial_affection,
                trust_score=initial_trust,
                created_at=timestamp,
                updated_at=timestamp,
                last_automatic_change_at=None,
            )
            .on_conflict_do_nothing(index_elements=["user_id"])
        )
        row = await session.get(PersonRelationshipModel, user_id)
        if row is None:
            raise IdentityDualWriteError("unclassified")
    return row


async def _ensure_group(
    session: AsyncSession,
    group_id: str,
    *,
    name: str = "",
    enabled: bool | None = None,
    now: datetime | None = None,
) -> GroupModel:
    timestamp = now or datetime.now(UTC)
    await require_v1_runtime(session)
    group = await session.get(GroupModel, group_id)
    if group is None:
        await session.execute(
            insert(GroupModel)
            .values(
                group_id=group_id,
                name=name,
                enabled=bool(enabled),
                require_mention=True,
                autonomous_enabled=True,
                first_seen_at=timestamp,
                last_seen_at=timestamp,
                updated_at=timestamp,
            )
            .on_conflict_do_nothing(index_elements=["group_id"])
        )
        group = await session.get(GroupModel, group_id)
        if group is None:
            raise IdentityDualWriteError("unclassified")
    if name:
        group.name = name
    if enabled is not None:
        group.enabled = enabled
    group.last_seen_at = timestamp
    group.updated_at = timestamp
    await sync_space(
        session,
        group_id,
        name=group.name,
        enabled=group.enabled,
        autonomous_enabled=group.autonomous_enabled,
        require_mention=group.require_mention,
        now=timestamp,
    )
    return group


def _event_record(row: ChatEventModel) -> EventRecord:
    try:
        decoded = json.loads(row.segments_json)
    except json.JSONDecodeError:
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
    if row.external_payload_json:
        try:
            raw_payload = json.loads(row.external_payload_json)
        except json.JSONDecodeError:
            raw_payload = None
        if isinstance(raw_payload, dict):
            external_payload = raw_payload
    return EventRecord(
        id=row.id,
        bot_user_id=row.bot_user_id,
        platform_message_id=row.platform_message_id,
        scope_type=ScopeType(row.scope_type),
        sender_user_id=row.sender_user_id,
        sender_nickname=row.sender_nickname,
        sender_group_card=row.sender_group_card,
        direction=row.direction,
        content=row.content,
        visual_summary=row.visual_summary,
        segments=segments,
        occurred_at=row.occurred_at,
        group_id=row.group_id,
        private_peer_user_id=row.private_peer_user_id,
        reply_to_message_id=row.reply_to_message_id,
        origin=row.origin,
        automation_id=row.automation_id,
        automation_run_id=row.automation_run_id,
        mentioned_user_ids=mentioned_user_ids,
        reply_sender_user_id=str(raw_reply_sender) if raw_reply_sender else None,
        event_kind=row.event_kind,
        source_plugin_id=row.source_plugin_id,
        external_source=row.external_source,
        external_event_key=row.external_event_key,
        external_event_type=row.external_event_type,
        external_payload=external_payload,
    )


def _relationship_snapshot(
    row: PersonRelationshipModel,
    *,
    trust_cap_offset: int,
) -> RelationshipSnapshot:
    usable_trust = effective_trust(
        row.affection_score,
        row.trust_score,
        cap_offset=trust_cap_offset,
    )
    return RelationshipSnapshot(
        user_id=row.user_id,
        affection_score=row.affection_score,
        trust_score=row.trust_score,
        effective_trust=usable_trust,
        relationship_weight=relationship_weight(row.affection_score, usable_trust),
        stage=stage_for_score(row.affection_score),
        updated_at=row.updated_at,
    )


def _relationship_event_record(row: RelationshipEventModel) -> RelationshipEventRecord:
    return RelationshipEventRecord(
        id=row.id,
        user_id=row.user_id,
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
