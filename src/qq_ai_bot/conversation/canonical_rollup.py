"""Canonical conversation rollup signal. Does not write conversation_scopes."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from qq_ai_bot.conversation.canonical_db_models import (
    CanonicalConversationModel,
    CanonicalConversationRollupJobModel,
    CanonicalConversationRollupModel,
)
from qq_ai_bot.conversation.rollup.models import RollupPolicyConfig
from qq_ai_bot.conversation.rollup.repository import (
    eligible_prefix,
    exceeds_high_watermark,
)
from qq_ai_bot.persistence.models import ChatEventModel
from qq_ai_bot.persistence.repository_helpers import _event_record, keeper_event_clause


def _utcnow() -> datetime:
    return datetime.now(UTC)


async def signal_canonical_rollup_if_needed(
    session: AsyncSession,
    conversation: CanonicalConversationModel,
    config: RollupPolicyConfig,
    *,
    force_existing: bool,
) -> bool:
    job = await session.get(CanonicalConversationRollupJobModel, conversation.id)
    now = _utcnow()
    if job is not None:
        if force_existing:
            job.signal_revision += 1
            job.next_attempt_at = now
            job.updated_at = now
            return True
        return False
    rollup = await session.get(CanonicalConversationRollupModel, conversation.id)
    coverage = (
        rollup.covered_through_event_id
        if rollup is not None and rollup.generation == conversation.generation
        else conversation.starts_after_event_id
    )
    rows = tuple(
        (
            await session.scalars(
                select(ChatEventModel)
                .where(
                    ChatEventModel.canonical_conversation_id == conversation.id,
                    ChatEventModel.id > coverage,
                    ChatEventModel.id <= conversation.last_event_id,
                    keeper_event_clause(),
                )
                .order_by(ChatEventModel.id.asc())
            )
        ).all()
    )
    events = tuple(_event_record(row) for row in rows)
    if not exceeds_high_watermark(eligible_prefix(events, config), config):
        return False
    session.add(
        CanonicalConversationRollupJobModel(
            conversation_id=conversation.id,
            generation=conversation.generation,
            signal_revision=1,
            status="pending",
            failure_count=0,
            lease_owner=None,
            lease_token=None,
            lease_until=None,
            next_attempt_at=now,
            last_error_category=None,
            created_at=now,
            updated_at=now,
        )
    )
    return True
