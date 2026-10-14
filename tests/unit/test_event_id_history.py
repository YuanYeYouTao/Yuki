"""The local history tool uses permanent event IDs across Presence changes."""

from __future__ import annotations

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel
from qq_ai_bot.domain.conversations import ConversationScope, ScopeType
from qq_ai_bot.identity.canonical_repository import ensure_person, ensure_presence
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.models import ChatEventModel
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


@pytest.mark.asyncio
async def test_history_around_keeps_one_snapshot_and_rejects_duplicate_center(
    database, monkeypatch
):
    ledger = EventLedgerRepository(database)
    events = []
    for index in range(3):
        row, _ = await ledger.append(
            bot_user_id="8000",
            scope_type=ScopeType.PRIVATE,
            sender_user_id="10001",
            direction="inbound",
            platform_message_id=f"snapshot-{index}",
            content=f"original-{index}",
        )
        events.append(row)
    scalar = AsyncSession.scalar
    raced = False

    async def reset_after_center(session, statement, *args, **kwargs):
        nonlocal raced
        result = await scalar(session, statement, *args, **kwargs)
        if isinstance(result, ChatEventModel) and result.id == events[1].id and not raced:
            raced = True
            async with database.immediate_session() as writer:
                await writer.execute(
                    update(CanonicalConversationModel)
                    .where(CanonicalConversationModel.id == events[1].canonical_conversation_id)
                    .values(
                        generation=2,
                        starts_after_event_id=events[-1].id,
                    )
                )
        return result

    monkeypatch.setattr(AsyncSession, "scalar", reset_after_center)
    scope = ConversationScope.private("8000", "10001")
    center, earlier, later = await ledger.list_scope_around(
        scope, event_id=events[1].id, before=1, after=1
    )
    assert raced and center.id == events[1].id
    assert [row.id for row in earlier] == [events[0].id]
    assert [row.id for row in later] == [events[2].id]
    assert (await ledger.list_scope_around(scope, event_id=center.id, before=1, after=1))[0] is None
    duplicate, _ = await ledger.append(
        bot_user_id="8000",
        scope_type=ScopeType.PRIVATE,
        sender_user_id="10001",
        direction="inbound",
        platform_message_id="duplicate-center",
        content="current generation",
    )
    async with database.immediate_session() as writer:
        await writer.execute(
            update(ChatEventModel)
            .where(ChatEventModel.id == duplicate.id)
            .values(suppression_status="duplicate", utterance_fingerprint="d" * 64)
        )
    assert (await ledger.list_scope_around(scope, event_id=duplicate.id, before=1, after=1))[
        0
    ] is None
