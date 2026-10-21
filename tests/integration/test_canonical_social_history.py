"""History follows stored entries and original receipts without remote credentials."""

from dataclasses import replace
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from tests.support.canonical_identity import ensure_person, ensure_presence
from tests.support.event_seed import seed_scoped_event
from tests.support.social_identity_cases import social_env

from yuki.capabilities.invocation import ToolInvocationContext, current_invocation
from yuki.conversation.canonical_db_models import CanonicalConversationModel
from yuki.domain.conversations import ConversationScope
from yuki.identity.db_models import CanonicalPersonModel, IdentityBindingModel
from yuki.identity.person_identity import merge_persons
from yuki.persistence.models import ChatEventModel
from yuki.services.agent_tools import ToolRuntime
from yuki.social.agent_adapter import invoke_social
from yuki.social.models import SocialError


async def private_entry(env, account, message_id):
    return (
        await seed_scoped_event(
            env.service.writer,
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


@pytest.mark.parametrize("source_disabled", [False, True])
async def test_history_discovers_original_entries_after_person_merge(
    database, tmp_path, source_disabled
):
    env = await social_env(database, tmp_path)
    original = await private_entry(env, "80001", "original-entry")
    other = (
        await seed_scoped_event(
            env.service.writer,
            scope=ConversationScope.private("80001", "10002"),
            platform_message_id="target-entry",
            sender_user_id="10002",
            direction="inbound",
            content="target entry history",
        )
    ).event
    async with database.immediate_session() as session:
        target = await ensure_person(session, "10002")
        original_binding = await session.scalar(
            select(IdentityBindingModel.id).where(IdentityBindingModel.person_id == env.person)
        )
        if source_disabled:
            (await session.get(CanonicalPersonModel, env.person)).enabled = False
        await merge_persons(session, env.person, target)
    env.registry.disconnect(env.bot)
    env.bot.calls.clear()
    entries = await env.service.execute(
        "read_conversation_history", {"kind": "person", "target_id": target}, env.context
    )
    assert entries["error"] == "conversation_ambiguous"
    assert {row["conversation_id"] for row in entries["conversations"]} == {
        original.canonical_conversation_id,
        other.canonical_conversation_id,
    }
    result = await env.service.execute(
        "read_conversation_history",
        {"kind": "person", "target_id": target, "binding_id": original_binding},
        env.context,
    )
    assert result["conversation_id"] == original.canonical_conversation_id
    assert result["target_id"] == target
    assert result["messages"][0]["person_id"] == env.person
    assert env.bot.calls == []


@pytest.mark.parametrize("grant", ["original", "current"])
async def test_delegated_history_keeps_same_person_scope_after_merge(database, tmp_path, grant):
    env = await social_env(database, tmp_path)
    original = await private_entry(env, "80001", "original-entry")
    await seed_scoped_event(
        env.service.writer,
        scope=ConversationScope.group("80001", "20001"),
        platform_message_id="known-foreign-person",
        sender_user_id="1003",
        direction="inbound",
        content="separate person",
    )
    async with database.immediate_session() as session:
        target = await ensure_person(session, "10002")
        foreign = await ensure_person(session, "1003")
        original_binding = await session.scalar(
            select(IdentityBindingModel.id).where(IdentityBindingModel.person_id == env.person)
        )
        if grant == "current":
            (await session.get(CanonicalPersonModel, env.person)).enabled = False
        await merge_persons(session, env.person, target)
    runtime = ToolRuntime(
        inbound=None,
        conversation_id=original.canonical_conversation_id,
        execution_id="original-delegated-read",
        read_scope=ConversationScope.private("80001", "10001"),
        read_target_id=env.person if grant == "original" else target,
    )
    env.bot.calls.clear()
    token = current_invocation.set(ToolInvocationContext(runtime, call_id="delegated"))
    try:
        result = await invoke_social(
            env.service,
            "read_conversation_history",
            {"kind": "person", "target_id": target, "binding_id": original_binding},
            runtime,
        )
        assert result["conversation_id"] == original.canonical_conversation_id
        assert result["messages"][0]["person_id"] == env.person
        with pytest.raises(SocialError, match="history_scope_denied"):
            await invoke_social(
                env.service, "find_contacts", {"kind": "person", "target_id": foreign}, runtime
            )
        with pytest.raises(SocialError, match="history_scope_denied"):
            await invoke_social(
                env.service,
                "read_conversation_history",
                {"conversation_id": env.context.conversation_id},
                runtime,
            )
    finally:
        current_invocation.reset(token)
    assert env.bot.calls == []
