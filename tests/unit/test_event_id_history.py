"""The local history tool uses permanent event IDs across Presence changes."""

from __future__ import annotations

import pytest

from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel
from qq_ai_bot.domain.conversations import ConversationScope, ScopeType
from qq_ai_bot.identity.canonical_repository import ensure_person, ensure_presence
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.repositories import EventLedgerRepository


@pytest.mark.asyncio
async def test_history_around_keeps_original_event_across_presence_switch(
    database: Database,
) -> None:
    async with database.immediate_session() as session:
        await ensure_person(session, "10001")
        await ensure_person(session, "10002")
        await ensure_presence(session, "8000")
        await ensure_presence(session, "8001")
    ledger = EventLedgerRepository(database)
    old, _ = await ledger.append(
        bot_user_id="8000",
        scope_type=ScopeType.PRIVATE,
        sender_user_id="10001",
        direction="inbound",
        platform_message_id="same-platform-id",
        content="old Presence",
    )
    await ledger.append(
        bot_user_id="8001",
        scope_type=ScopeType.PRIVATE,
        sender_user_id="10001",
        direction="inbound",
        platform_message_id="new-message",
        content="new Presence",
    )
    collision, _ = await ledger.append(
        bot_user_id="8001",
        scope_type=ScopeType.PRIVATE,
        sender_user_id="10002",
        direction="inbound",
        platform_message_id="same-platform-id",
        content="other conversation",
    )
    new_scope = ConversationScope.private("8001", "10001")
    center, _, later = await ledger.list_scope_around(
        new_scope, event_id=old.id, before=0, after=2, message_only=True
    )
    assert center is not None and center.id == old.id
    assert [row.content for row in later] == ["new Presence"]
    assert (
        await ledger.list_scope_around(
            new_scope, event_id=collision.id, before=0, after=0, message_only=True
        )
    )[0] is None
    assert old.canonical_conversation_id is not None
    async with database.immediate_session() as session:
        conversation = await session.get(CanonicalConversationModel, old.canonical_conversation_id)
        assert conversation is not None
        conversation.starts_after_event_id = old.id
        conversation.covered_through_event_id = old.id
    assert (
        await ledger.list_scope_around(
            new_scope, event_id=old.id, before=0, after=0, message_only=True
        )
    )[0] is None
    assert (
        await ledger.get_reply_event(
            old.id,
            conversation_id=old.canonical_conversation_id,
            current_generation_only=True,
        )
        is None
    )
