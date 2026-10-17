"""Canonical identity and public history fixtures."""

from __future__ import annotations

from datetime import UTC, datetime

from yuki.domain.conversations import ConversationScope, ScopeType
from yuki.persistence.database import Database
from yuki.persistence.scoped_event_uow import ScopedEventLedgerUnitOfWork


async def _append(
    uow: ScopedEventLedgerUnitOfWork,
    scope: ConversationScope,
    count: int,
    *,
    start: int = 1,
    actor_prefix: str = "member",
    origin: str = "user_message",
) -> None:
    from yuki.identity.canonical_repository import ensure_person, ensure_presence, ensure_space

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
    from yuki.identity.canonical_repository import ensure_person, ensure_presence

    async with database.sessions() as session, session.begin():
        await ensure_presence(session, bot)
        await ensure_person(session, peer, now=_NOW)
    return ConversationScope.private(bot, peer)


async def _publish_checkpoint(database, conversation_id, coverage, text="selected checkpoint"):
    from uuid import uuid4

    from yuki.conversation.canonical_db_models import CanonicalConversationModel
    from yuki.conversation.projection_models import PromptProjectionModel

    async with database.immediate_session() as session:
        owner = await session.get(CanonicalConversationModel, conversation_id)
        session.add(
            PromptProjectionModel(
                view_key=owner.id,
                conversation_id=owner.id,
                generation=owner.generation,
                source_revision=owner.prompt_source_revision,
                starts_after_event_id=owner.starts_after_event_id,
                epoch_id=str(uuid4()),
                context_key="fixture",
                revision=1,
                rebuild_reason="compact",
                payload_json="[]",
                byte_size=2,
                updated_at=datetime.now(UTC),
                selected_summary_text=text,
                selected_summary_coverage=coverage,
                selected_summary_kind="compact",
                selected_summary_renderer=2,
            )
        )
