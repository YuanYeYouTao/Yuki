"""Canonical conversation rollup signal. Does not write conversation_scopes."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from sqlalchemy import delete, select
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.ext.asyncio import AsyncSession

from qq_ai_bot.conversation.canonical_db_models import (
    CanonicalConversationModel,
    CanonicalConversationRollupJobModel,
    CanonicalConversationRollupModel,
)
from qq_ai_bot.conversation.hydrate import require_primary_alias_for_conversation
from qq_ai_bot.conversation.rollup.models import RollupPolicyConfig
from qq_ai_bot.conversation.rollup.signals import signals
from qq_ai_bot.domain.conversations import ConversationScope
from qq_ai_bot.event_prompt import ChatEventPromptRenderer
from qq_ai_bot.model_runtime.capacity import estimate_text_tokens
from qq_ai_bot.persistence.database import Database
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
    await session.execute(
        insert(signals)
        .values(
            conversation_id=conversation.id,
            generation=conversation.generation,
            event_id=conversation.last_event_id,
        )
        .on_conflict_do_update(
            index_elements=[signals.c.conversation_id],
            set_={
                "generation": conversation.generation,
                "event_id": conversation.last_event_id,
                "revision": signals.c.revision + 1,
            },
        )
    )
    return True


async def drain_rollup_signals(
    database: Database,
    config: RollupPolicyConfig,
    *,
    policy_for_scope: Callable[[ConversationScope], Awaitable[RollupPolicyConfig]] | None = None,
) -> None:
    async with database.sessions() as session:
        pending = (await session.execute(select(signals).limit(8))).mappings().all()
    for signal in pending:
        policy = config
        if policy_for_scope is not None:
            async with database.sessions() as session:
                exists = await session.get(CanonicalConversationModel, signal["conversation_id"])
                alias = (
                    await require_primary_alias_for_conversation(session, signal["conversation_id"])
                    if exists is not None
                    else None
                )
            if alias is not None:
                policy = await policy_for_scope(ConversationScope.parse(alias))
        async with database.sessions() as session:
            conversation = await session.get(CanonicalConversationModel, signal["conversation_id"])
            if conversation is None or conversation.generation != signal["generation"]:
                needed = False
            else:
                needed = await _needs_rollup(session, conversation, policy)
        async with database.immediate_session() as session:
            removed = await session.execute(
                delete(signals)
                .where(
                    signals.c.conversation_id == signal["conversation_id"],
                    signals.c.generation == signal["generation"],
                    signals.c.event_id == signal["event_id"],
                    signals.c.revision == signal["revision"],
                )
                .returning(signals.c.conversation_id)
            )
            if removed.first() is None:
                continue
            current = await session.get(CanonicalConversationModel, signal["conversation_id"])
            if needed and current is not None and current.generation == signal["generation"]:
                now = _utcnow()
                await session.execute(
                    insert(CanonicalConversationRollupJobModel)
                    .values(
                        conversation_id=current.id,
                        generation=current.generation,
                        signal_revision=1,
                        status="pending",
                        failure_count=0,
                        next_attempt_at=now,
                        created_at=now,
                        updated_at=now,
                    )
                    .on_conflict_do_nothing(index_elements=["conversation_id"])
                )


async def _needs_rollup(
    session: AsyncSession, conversation: CanonicalConversationModel, config: RollupPolicyConfig
) -> bool:
    rollup = await session.get(CanonicalConversationRollupModel, conversation.id)
    coverage = (
        rollup.covered_through_event_id
        if rollup is not None and rollup.generation == conversation.generation
        else conversation.starts_after_event_id
    )
    # Read bounded pages and stop as soon as the capacity trigger is proven.
    # Counts remain diagnostics; small messages never acquire a lifetime cap.
    tokens = 0
    cursor = coverage
    threshold = int(config.context_token_budget * config.trigger_ratio)
    while True:
        rows = tuple(
            (
                await session.scalars(
                    select(ChatEventModel)
                    .where(
                        ChatEventModel.canonical_conversation_id == conversation.id,
                        ChatEventModel.id > cursor,
                        ChatEventModel.id <= conversation.last_event_id,
                        keeper_event_clause(),
                    )
                    .order_by(ChatEventModel.id.asc())
                    .limit(config.batch_max_events)
                )
            ).all()
        )
        if not rows:
            return False
        for row in rows:
            event = _event_record(row)
            messages = ChatEventPromptRenderer(
                (event,), bot_display_name=config.bot_display_name, timezone=config.timezone
            ).main_agent_history((event,))
            tokens += sum(
                estimate_text_tokens(message.content or "") + 8 for _, _, message in messages
            )
            if tokens >= threshold:
                return True
        cursor = rows[-1].id
