"""Durable causality at the canonical event-ledger write boundary."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationRollupModel
from qq_ai_bot.conversation.rollup.models import RollupPolicyConfig
from qq_ai_bot.conversation.rollup.repository import ConversationRollupRepository
from qq_ai_bot.domain.conversations import ConversationScope
from qq_ai_bot.event_prompt import ChatEventPromptRenderer
from qq_ai_bot.identity.errors import CanonicalIdentityError
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.models import ChatEventModel
from qq_ai_bot.persistence.scoped_event_uow import ScopedEventLedgerUnitOfWork


def _writer(database: Database) -> ScopedEventLedgerUnitOfWork:
    return ScopedEventLedgerUnitOfWork(database, config=RollupPolicyConfig())


async def _external_event(
    writer: ScopedEventLedgerUnitOfWork,
    *,
    peer: str = "1001",
    key: str = "evt-1",
) -> int:
    result = await writer.append_external(
        scope=ConversationScope.private("8000", peer),
        platform_message_id=f"external:{key}",
        source_plugin_id="test.causality",
        external_source="fixture",
        external_event_key=key,
        external_event_type="fixture.created",
        external_payload={"id": key},
        external_target_id=peer,
        content="fixture event",
        occurred_at=datetime(2026, 8, 28, tzinfo=UTC),
    )
    return result.event.id


@pytest.mark.asyncio
async def test_plugin_outbound_requires_and_projects_same_conversation_cause(
    database: Database,
) -> None:
    writer = _writer(database)
    source_event_id = await _external_event(writer)

    result = await writer.append(
        scope=ConversationScope.private("8000", "1001"),
        platform_message_id="reply-1",
        sender_user_id="8000",
        direction="outbound",
        content="proactive reply",
        sender_is_bot=True,
        origin="plugin_background",
        caused_by_event_id=source_event_id,
    )

    assert result.event.caused_by_event_id == source_event_id
    assert result.event.canonical_conversation_id is not None


@pytest.mark.asyncio
async def test_plugin_outbound_without_cause_fails_closed(database: Database) -> None:
    writer = _writer(database)

    with pytest.raises(CanonicalIdentityError) as exc:
        await writer.append(
            scope=ConversationScope.private("8000", "1001"),
            platform_message_id="reply-without-cause",
            sender_user_id="8000",
            direction="outbound",
            content="orphan reply",
            sender_is_bot=True,
            origin="plugin_background",
        )

    assert exc.value.category == "causal_source_required"


@pytest.mark.asyncio
async def test_plugin_outbound_rejects_cross_conversation_cause(database: Database) -> None:
    writer = _writer(database)
    source_event_id = await _external_event(writer, peer="1002", key="other-conversation")

    with pytest.raises(CanonicalIdentityError) as exc:
        await writer.append(
            scope=ConversationScope.private("8000", "1001"),
            platform_message_id="reply-cross-conversation",
            sender_user_id="8000",
            direction="outbound",
            content="wrong cause",
            sender_is_bot=True,
            origin="plugin_background",
            caused_by_event_id=source_event_id,
        )

    assert exc.value.category == "causal_source_invalid"


@pytest.mark.asyncio
async def test_raw_reply_hydrates_cause_metadata_after_source_is_covered(
    database: Database,
) -> None:
    writer = _writer(database)
    source_event_id = await _external_event(writer, key="covered-source")
    async with database.immediate_session() as session:
        source = await session.get(ChatEventModel, source_event_id)
        assert source is not None and source.canonical_conversation_id is not None
        conversation_id = source.canonical_conversation_id
        session.add(
            CanonicalConversationRollupModel(
                conversation_id=conversation_id,
                generation=1,
                covered_through_event_id=source_event_id,
                summary_text="covered external source",
                summary_kind="model",
                source_fingerprint="c" * 64,
                revision=1,
                created_at=datetime(2026, 8, 28, tzinfo=UTC),
                updated_at=datetime(2026, 8, 28, tzinfo=UTC),
            )
        )
    await writer.append(
        scope=ConversationScope.private("8000", "1001"),
        platform_message_id="reply-after-covered-source",
        sender_user_id="8000",
        direction="outbound",
        content="proactive after coverage",
        sender_is_bot=True,
        origin="plugin_background",
        caused_by_event_id=source_event_id,
    )

    snapshot = await ConversationRollupRepository(
        database,
        RollupPolicyConfig(),
    ).load_prompt_snapshot(ConversationScope.private("8000", "1001"))

    assert len(snapshot.raw_events) == 1
    reply = snapshot.raw_events[0]
    assert reply.caused_by_event_id == source_event_id
    assert reply.caused_by_external_source == "fixture"
    assert reply.caused_by_external_event_type == "fixture.created"
    projected = ChatEventPromptRenderer(snapshot.raw_events).main_agent_history(snapshot.raw_events)
    assert "source=fixture" in (projected[0][2].content or "")
    assert "type=fixture.created" in (projected[0][2].content or "")
