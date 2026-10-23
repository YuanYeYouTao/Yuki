"""History follows stored entries and original receipts without remote credentials."""

from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from tests.support.canonical_identity import ensure_person, ensure_presence
from tests.support.event_seed import seed_scoped_event
from tests.support.person_merge import merge_persons
from tests.support.social_history_cases import history_agent_loop, stored_private_entry
from tests.support.social_identity_cases import social_env

from yuki.capabilities.invocation import ToolInvocationContext, current_invocation
from yuki.conversation.canonical_db_models import CanonicalConversationModel
from yuki.domain.conversations import ConversationScope
from yuki.identity.canonical_repository import (
    find_identity_binding,
    replace_assignment,
)
from yuki.identity.db_models import AccountModel, CanonicalPersonModel
from yuki.persistence.models import ChatEventModel
from yuki.persistence.repositories import EventLedgerRepository
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


async def test_same_person_multiple_entries_query_together_without_merging_sources(
    database, tmp_path
):
    env = await social_env(database, tmp_path)
    async with database.immediate_session() as session:
        await ensure_presence(session, "80002")
    first = await private_entry(env, "80001", "entry-a")
    second = await private_entry(env, "80002", "entry-b")
    assert first.canonical_conversation_id != second.canonical_conversation_id
    env.registry.disconnect(env.bot)
    env.bot.calls.clear()
    result = await env.service.execute(
        "query_chat_history",
        {"view": "recent", "event_kind": "all", "target": {"person_id": env.person}},
        env.context,
    )
    assert [row["event_id"] for row in result["events"]] == [second.id, first.id]
    assert {row["conversation_id"] for row in result["sources"]} == {
        first.canonical_conversation_id,
        second.canonical_conversation_id,
    }
    result = await env.service.execute(
        "query_chat_history",
        {
            "view": "recent",
            "event_kind": "all",
            "target": {"conversation_id": second.canonical_conversation_id},
        },
        env.context,
    )
    assert result["source"] == "canonical_ledger"
    assert [row["event_id"] for row in result["events"]] == [second.id]
    assert result["events"][0]["person_id"] == env.person
    assert env.bot.calls == []


async def test_receipt_and_delegated_history_keep_actual_source_grants(database, tmp_path):
    env = await social_env(database, tmp_path)
    receipt = await env.service.execute("send", {"text": "confirmed"}, env.context)
    private = await private_entry(env, "80001", "private-entry")
    context = replace(env.context, conversation_id=private.canonical_conversation_id)
    env.bot.calls.clear()
    with pytest.raises(SocialError, match="history_receipt_unavailable"):
        await env.service.execute(
            "query_chat_history",
            {
                "view": "recent",
                "event_kind": "all",
                "target": {"operation_id": receipt["operation_id"]},
            },
            context,
        )
    unavailable = await env.service.execute(
        "query_chat_history",
        {
            "view": "around",
            "event_id": receipt["event_id"],
            "target": {"conversation_id": private.canonical_conversation_id},
        },
        context,
    )
    assert unavailable["anchor_status"] == "unavailable" and not unavailable["events"]
    token = current_invocation.set(
        ToolInvocationContext(
            SimpleNamespace(read_target_id=env.person), invocation_key="delegated"
        )
    )
    try:
        with pytest.raises(SocialError, match="history_scope_denied"):
            await env.service.execute(
                "query_chat_history", {"view": "recent", "event_kind": "all"}, env.context
            )
    finally:
        current_invocation.reset(token)
    env.registry.disconnect(env.bot)
    original = await env.service.execute(
        "query_chat_history",
        {
            "view": "recent",
            "event_kind": "all",
            "target": {"operation_id": receipt["operation_id"]},
        },
        env.context,
    )
    assert any(row["event_id"] == receipt["event_id"] for row in original["events"])
    assert env.bot.calls == []


async def test_permanent_group_history_survives_new_context_window(database, tmp_path):
    env = await social_env(database, tmp_path)
    sent = await env.service.execute("send", {"text": "after reset"}, env.context)
    async with database.immediate_session() as session:
        source = await session.scalar(
            select(ChatEventModel).where(ChatEventModel.direction == "inbound")
        )
        conversation = await session.get(CanonicalConversationModel, env.context.conversation_id)
        conversation.starts_after_event_id = source.id
    env.bot.calls.clear()
    result = await env.service.execute(
        "query_chat_history", {"view": "recent", "event_kind": "all"}, env.context
    )
    assert [row["event_id"] for row in result["events"]] == [sent["event_id"], source.id]
    assert "after reset" in result["events"][0]["content"]
    assert env.bot.calls == []


async def test_main_history_empty_arguments_use_recent_without_schema_rejection(database, tmp_path):
    env = await social_env(database, tmp_path)
    await history_agent_loop(env, arguments="{}")


@pytest.mark.parametrize("platform", ["qq", "telegram", "feishu"])
async def test_platform_personal_history_and_cross_entry_binding_survive_new_window(
    database, tmp_path, platform
):
    env = await social_env(database, tmp_path)
    entry = await stored_private_entry(env, platform)
    original = entry.event
    async with database.immediate_session() as session:
        conversation = await session.get(
            CanonicalConversationModel, original.canonical_conversation_id
        )
        conversation.starts_after_event_id = original.id
        conversation.generation += 1
    current = (
        await seed_scoped_event(
            env.service.writer,
            scope=entry.scope,
            platform_message_id="after-new",
            sender_user_id=entry.scope.private_peer_user_id,
            direction="inbound",
            content="现在重新聊",
        )
    ).event
    context = replace(
        env.context,
        conversation_id=original.canonical_conversation_id,
        space_id=None,
        actor=SimpleNamespace(person_id=env.person, principal_kind="person"),
    )
    recent = await env.service.execute("query_chat_history", {"view": "recent"}, context)
    assert [row["event_id"] for row in recent["events"]] == [current.id, original.id]
    found = await env.service.execute(
        "query_chat_history", {"view": "search", "keyword": "雷达站"}, context
    )
    assert [row["event_id"] for row in found["events"]] == [original.id]
    around = await env.service.execute(
        "query_chat_history", {"view": "around", "event_id": original.id, "after": 1}, context
    )
    assert [row["event_id"] for row in around["events"]] == [original.id, current.id]
    cross_entry = await env.service.execute(
        "query_chat_history",
        {"view": "search", "keyword": "雷达站", "target": {"binding_id": entry.binding_id}},
        replace(env.context, actor=SimpleNamespace(person_id=env.person, principal_kind="person")),
    )
    assert cross_entry["events"] == found["events"]
    assert cross_entry["platform"] == platform
    assert cross_entry["events"][0]["person_id"] == env.person
    version, automatic_context = await EventLedgerRepository(database).read_scope_context(
        entry.scope, limit=20
    )
    assert [row.id for row in automatic_context] == [current.id]
    assert version.generation == 2 and version.starts_after_event_id == original.id
    async with database.read_session() as session:
        conversation = await session.get(
            CanonicalConversationModel, original.canonical_conversation_id
        )
        assert conversation.generation == 2
        assert conversation.starts_after_event_id == original.id


async def test_account_transfer_keeps_original_binding_conversation_and_author(database, tmp_path):
    env = await social_env(database, tmp_path)
    original = await private_entry(env, "80001", "before-transfer")
    async with database.immediate_session() as session:
        old_binding = await find_identity_binding(session, "10001")
        old_binding_id = old_binding.id
        target = await ensure_person(session, "10002")
        account = await session.get(AccountModel, old_binding.account_id)
        new_binding = await replace_assignment(
            session,
            account,
            old_binding_id,
            await session.get(CanonicalPersonModel, target),
            now=datetime.now(UTC),
        )
        new_binding_id = new_binding.id
        conversation = await session.get(
            CanonicalConversationModel, original.canonical_conversation_id
        )
        conversation.starts_after_event_id = original.id
    current = await private_entry(env, "80001", "after-transfer")
    assert current.canonical_conversation_id != original.canonical_conversation_id
    for binding_id, expected in ((old_binding_id, original), (new_binding_id, current)):
        result = await env.service.execute(
            "query_chat_history",
            {"view": "recent", "target": {"binding_id": binding_id}},
            env.context,
        )
        assert result["conversation_id"] == expected.canonical_conversation_id
        assert [row["event_id"] for row in result["events"]] == [expected.id]
        assert result["events"][0]["person_id"] == expected.author_person_id
    assert original.author_person_id == env.person
    assert current.author_person_id == target


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
        if source_disabled:
            (await session.get(CanonicalPersonModel, env.person)).enabled = False
        await merge_persons(session, env.person, target)
    env.registry.disconnect(env.bot)
    env.bot.calls.clear()
    entries = await env.service.execute(
        "query_chat_history",
        {"view": "recent", "event_kind": "all", "target": {"person_id": target}},
        env.context,
    )
    assert [row["event_id"] for row in entries["events"]] == [other.id, original.id]
    assert {row["conversation_id"] for row in entries["sources"]} == {
        original.canonical_conversation_id,
        other.canonical_conversation_id,
    }
    result = await env.service.execute(
        "query_chat_history",
        {
            "view": "recent",
            "event_kind": "all",
            "target": {"conversation_id": original.canonical_conversation_id},
        },
        env.context,
    )
    assert result["conversation_id"] == original.canonical_conversation_id
    assert result["target_id"] == target
    assert result["events"][0]["person_id"] == env.person
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
    token = current_invocation.set(ToolInvocationContext(runtime, invocation_key="delegated"))
    try:
        result = await invoke_social(
            env.service,
            "query_chat_history",
            {"view": "recent", "target": {"conversation_id": original.canonical_conversation_id}},
            runtime,
        )
        assert result["conversation_id"] == original.canonical_conversation_id
        assert result["events"][0]["person_id"] == env.person
        with pytest.raises(SocialError, match="history_scope_denied"):
            await invoke_social(
                env.service, "find_contacts", {"kind": "person", "target_id": foreign}, runtime
            )
        with pytest.raises(SocialError, match="history_scope_denied"):
            await invoke_social(
                env.service,
                "query_chat_history",
                {"view": "recent", "target": {"conversation_id": env.context.conversation_id}},
                runtime,
            )
    finally:
        current_invocation.reset(token)
    assert env.bot.calls == []
