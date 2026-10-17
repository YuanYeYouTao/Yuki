"""History follows stored entries and original receipts without remote credentials."""

from dataclasses import replace
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from tests.support.social_identity_cases import social_env

from qq_ai_bot.capabilities.invocation import ToolInvocationContext, current_invocation
from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel
from qq_ai_bot.domain.conversations import ConversationScope
from qq_ai_bot.identity.canonical_repository import ensure_presence
from qq_ai_bot.persistence.models import ChatEventModel
from qq_ai_bot.social.models import SocialError


async def private_entry(env, account, message_id):
    return (
        await env.service.writer.append(
            scope=ConversationScope.private(account, "10001"),
            platform_message_id=message_id,
            sender_user_id="10001",
            direction="inbound",
            content="entry-local history",
        )
    ).event


async def test_same_person_multiple_entries_require_original_conversation(database, tmp_path):
    env = await social_env(database, tmp_path)
    async with database.immediate_session() as session:
        await ensure_presence(session, "80002")
    first = await private_entry(env, "80001", "entry-a")
    second = await private_entry(env, "80002", "entry-b")
    assert first.canonical_conversation_id != second.canonical_conversation_id
    env.registry.disconnect(env.bot)
    env.bot.calls.clear()
    result = await env.service.execute(
        "read_conversation_history",
        {"kind": "person", "target_id": env.person},
        env.context,
    )
    assert result["error"] == "conversation_ambiguous"
    assert {row["conversation_id"] for row in result["conversations"]} == {
        first.canonical_conversation_id,
        second.canonical_conversation_id,
    }
    result = await env.service.execute(
        "read_conversation_history",
        {"conversation_id": second.canonical_conversation_id},
        env.context,
    )
    assert result["source"] == "canonical_ledger"
    assert [row["event_id"] for row in result["messages"]] == [second.id]
    assert result["messages"][0]["person_id"] == env.person
    assert env.bot.calls == []


async def test_receipt_and_delegated_history_keep_actual_source_grants(database, tmp_path):
    env = await social_env(database, tmp_path)
    receipt = await env.service.execute("send_message", {"text": "confirmed"}, env.context)
    private = await private_entry(env, "80001", "private-entry")
    context = replace(env.context, conversation_id=private.canonical_conversation_id)
    env.bot.calls.clear()
    with pytest.raises(SocialError, match="history_receipt_unavailable"):
        await env.service.execute(
            "read_conversation_history", {"operation_id": receipt["operation_id"]}, context
        )
    with pytest.raises(SocialError, match="history_scope_denied"):
        await env.service.execute(
            "read_conversation_history", {"event_id": receipt["event_id"]}, context
        )
    token = current_invocation.set(
        ToolInvocationContext(SimpleNamespace(read_target_id=env.person), call_id="delegated")
    )
    try:
        with pytest.raises(SocialError, match="history_scope_denied"):
            await env.service.execute("read_conversation_history", {}, env.context)
    finally:
        current_invocation.reset(token)
    env.registry.disconnect(env.bot)
    original = await env.service.execute(
        "read_conversation_history", {"operation_id": receipt["operation_id"]}, env.context
    )
    assert any(row["event_id"] == receipt["event_id"] for row in original["messages"])
    assert env.bot.calls == []


async def test_history_respects_existing_reset_boundary(database, tmp_path):
    env = await social_env(database, tmp_path)
    sent = await env.service.execute("send_message", {"text": "after reset"}, env.context)
    async with database.immediate_session() as session:
        source = await session.scalar(
            select(ChatEventModel).where(ChatEventModel.direction == "inbound")
        )
        conversation = await session.get(CanonicalConversationModel, env.context.conversation_id)
        conversation.starts_after_event_id = source.id
    env.bot.calls.clear()
    result = await env.service.execute("read_conversation_history", {}, env.context)
    assert [row["event_id"] for row in result["messages"]] == [sent["event_id"]]
    assert "after reset" in result["messages"][0]["content"]
    assert env.bot.calls == []
