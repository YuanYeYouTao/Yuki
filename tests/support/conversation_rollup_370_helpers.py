"""Adversarial coverage for the 3.7 single-checkpoint rollup contract."""

from __future__ import annotations

from datetime import UTC, datetime

from qq_ai_bot.conversation.rollup.models import (
    RollupPolicyConfig,
)
from qq_ai_bot.domain.conversations import ConversationScope, ScopeType
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.scoped_event_uow import ScopedEventLedgerUnitOfWork


def _policy(*, batch_max_events: int = 100) -> RollupPolicyConfig:
    return RollupPolicyConfig(
        context_token_budget=100,
        batch_max_events=batch_max_events,
        batch_max_characters=100_000,
    )


async def _append(
    uow: ScopedEventLedgerUnitOfWork,
    scope: ConversationScope,
    count: int,
    *,
    start: int = 1,
    actor_prefix: str = "member",
    origin: str = "user_message",
) -> None:
    from qq_ai_bot.identity.canonical_repository import ensure_person, ensure_presence, ensure_space

    async with uow._database.sessions() as session, session.begin():
        await ensure_presence(session, scope.bot_user_id)
        if scope.scope_type is ScopeType.GROUP:
            assert scope.group_id is not None
            await ensure_space(session, scope.group_id)
        else:
            assert scope.private_peer_user_id is not None
            await ensure_person(session, scope.private_peer_user_id)
        for index in range(start, start + count):
            await ensure_person(session, f"{actor_prefix}-{index % 2}")
    for index in range(start, start + count):
        await uow.append(
            scope=scope,
            platform_message_id=f"message-{scope.bot_user_id}-{index}",
            sender_user_id=f"{actor_prefix}-{index % 2}",
            direction="inbound",
            content=f"event-{index}",
            occurred_at=datetime(2026, 8, 20, 0, index % 60, tzinfo=UTC),
            origin=origin,
        )


_NOW = datetime(2026, 8, 24, tzinfo=UTC)


async def _prepare_v2_private(
    database: Database, *, bot: str = "8000", peer: str = "1001"
) -> ConversationScope:
    from qq_ai_bot.identity.canonical_repository import ensure_person, ensure_presence

    async with database.sessions() as session, session.begin():
        await ensure_presence(session, bot)
        await ensure_person(session, peer, now=_NOW)
    return ConversationScope.private(bot, peer)
