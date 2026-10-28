"""Durable causality at the canonical event-ledger write boundary."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from yuki.conversation.hydrate import ensure_canonical_conversation
from yuki.domain.conversations import ConversationScope
from yuki.event_prompt import ChatEventPromptRenderer
from yuki.identity.canonical_repository import find_identity_binding, find_presence
from yuki.identity.errors import CanonicalIdentityError
from yuki.persistence.database import Database
from yuki.persistence.models import ChatEventModel
from yuki.persistence.scoped_event_uow import ScopedEventLedgerUnitOfWork


def _writer(database: Database) -> ScopedEventLedgerUnitOfWork:
    return ScopedEventLedgerUnitOfWork(database)


async def _scope(database: Database, peer: str) -> ConversationScope:
    async with database.immediate_session() as writer:
        binding = await find_identity_binding(writer, peer)
        presence = await find_presence(writer, "8000")
        conversation = await ensure_canonical_conversation(
            writer,
            kind="private",
            primary_scope_key=ConversationScope.private("8000", peer).key,
            person_id=binding.person_id,
            identity_binding_id=binding.id,
            presence_id=presence.id,
        )
    return ConversationScope.private(
        "8000", peer, presence_id=presence.id, conversation_id=conversation.conversation_id
    )


async def _external_event(
    writer: ScopedEventLedgerUnitOfWork,
    *,
    peer: str = "1001",
    key: str = "evt-1",
) -> int:
    result = await writer.append_external(
        scope=await _scope(writer._database, peer),
        origin_key=f"external:{key}",
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
        scope=await _scope(database, "1001"),
        origin_key="reply-1",
        sender_user_id="8000",
        direction="outbound",
        content="proactive reply",
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
            scope=await _scope(database, "1001"),
            origin_key="reply-without-cause",
            sender_user_id="8000",
            direction="outbound",
            content="orphan reply",
            origin="plugin_background",
        )

    assert exc.value.category == "causal_source_required"


@pytest.mark.asyncio
async def test_plugin_outbound_rejects_cross_conversation_cause(database: Database) -> None:
    writer = _writer(database)
    source_event_id = await _external_event(writer, peer="1002", key="other-conversation")

    with pytest.raises(CanonicalIdentityError) as exc:
        await writer.append(
            scope=await _scope(database, "1001"),
            origin_key="reply-cross-conversation",
            sender_user_id="8000",
            direction="outbound",
            content="wrong cause",
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
    from tests.support.public_history import _publish_checkpoint

    await _publish_checkpoint(database, conversation_id, source_event_id, "covered external source")
    await writer.append(
        scope=await _scope(database, "1001"),
        origin_key="reply-after-covered-source",
        sender_user_id="8000",
        direction="outbound",
        content="proactive after coverage",
        origin="plugin_background",
        caused_by_event_id=source_event_id,
    )

    from yuki.conversation.scope_repository import ConversationScopeRepository

    snapshot = await ConversationScopeRepository(database).load_prompt_snapshot(
        ConversationScope.private("8000", "1001")
    )

    assert len(snapshot.raw_events) == 1
    reply = snapshot.raw_events[0]
    assert reply.caused_by_event_id == source_event_id
    assert reply.caused_by_external_source == "fixture"
    assert reply.caused_by_external_event_type == "fixture.created"
    projected = ChatEventPromptRenderer(snapshot.raw_events).main_agent_history(snapshot.raw_events)
    assert "source=fixture" in (projected[0][2].content or "")
    assert "type=fixture.created" in (projected[0][2].content or "")
