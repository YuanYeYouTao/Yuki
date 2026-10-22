"""Main reads permanent observed events across entries while preserving their sources."""

from types import SimpleNamespace

import pytest
from sqlalchemy import select
from tests.support.canonical_identity import ensure_person
from tests.support.event_seed import seed_scoped_event
from tests.support.social_history_cases import stored_private_entry
from tests.support.social_identity_cases import social_env

from yuki.capabilities.invocation import ToolInvocationContext, current_invocation
from yuki.conversation.canonical_db_models import CanonicalConversationModel
from yuki.domain.conversations import ConversationScope
from yuki.identity.db_models import SpaceBindingModel
from yuki.persistence.models import ChatEventModel
from yuki.social.models import SocialError


@pytest.fixture
async def permanent_entries(database, tmp_path):
    env = await social_env(database, tmp_path)
    async with database.immediate_session() as session:
        binding = await session.scalar(
            select(SpaceBindingModel).where(SpaceBindingModel.space_id == env.space)
        )
        binding.display_name = "雷达讨论"
        other_person = await ensure_person(session, "10002")
    entries = [
        await stored_private_entry(
            env, platform, person_id=other_person if platform == "feishu" else env.person
        )
        for platform in ("qq", "telegram", "feishu")
    ]
    for thread in ("radar-north", "radar-south"):
        scope = ConversationScope.group("80001", "20001", thread_id=thread)
        added = await seed_scoped_event(
            env.service.writer,
            scope=scope,
            platform_message_id=thread,
            sender_user_id="10001",
            direction="inbound",
            content=f"雷达站 {thread}",
        )
        entries.append(SimpleNamespace(event=added.event, scope=scope))
    duplicate = await seed_scoped_event(
        env.service.writer,
        scope=entries[-1].scope,
        platform_message_id="duplicate-message",
        sender_user_id="10001",
        direction="inbound",
        content="雷达站 duplicate",
    )
    forgotten = await seed_scoped_event(
        env.service.writer,
        scope=entries[-1].scope,
        platform_message_id="forgotten-message",
        sender_user_id="10001",
        direction="inbound",
        content="雷达站 forgotten",
    )
    async with database.immediate_session() as session:
        for entry in entries:
            conversation = await session.get(
                CanonicalConversationModel, entry.event.canonical_conversation_id
            )
            conversation.starts_after_event_id = entry.event.id
            conversation.generation += 1
        row = await session.get(ChatEventModel, duplicate.event.id)
        row.suppression_status = "duplicate"
        row.utterance_fingerprint = "d" * 64
        await session.delete(await session.get(ChatEventModel, forgotten.event.id))
    env.registry.disconnect(env.bot)
    env.bot.calls.clear()
    return env, entries


@pytest.mark.parametrize("order", ["recent", "oldest"])
async def test_main_searches_all_permanent_platforms_and_threads_with_one_cursor(
    permanent_entries, order
):
    env, entries = permanent_entries
    expected = {entry.event.id: entry for entry in entries}
    observed = []
    request = {"view": "search", "keyword": "雷达站", "limit": 2, "order": order}
    while True:
        result = await env.service.execute("query_chat_history", request, env.context)
        assert result["source"] == "canonical_ledger" and result["coverage"] == "observed"
        sources = {source["conversation_id"]: source for source in result["sources"]}
        for row in result["events"]:
            entry = expected[row["event_id"]]
            observed.append(row["event_id"])
            assert row["conversation_id"] == entry.event.canonical_conversation_id
            assert row["person_id"] == entry.event.author_person_id
            assert row["platform"] == entry.scope.platform
            assert row["scope"] == entry.scope.scope_type.value
            assert row["thread_id"] == entry.scope.thread_id
            assert sources[row["conversation_id"]]["platform"] == entry.scope.platform
            expected_name = (
                f"{entry.scope.platform} friend"
                if entry.scope.scope_type.value == "private"
                else "雷达讨论"
            )
            assert row["conversation_name"] == expected_name
            assert sources[row["conversation_id"]]["conversation_name"] == expected_name
        assert set(result.source_event_ids) <= set(expected)
        if not result["has_more"]:
            break
        request = {**request, "cursor": result["next_cursor"]}
    assert observed == sorted(expected, reverse=order == "recent")
    assert env.bot.calls == []


async def test_owner_selectors_combine_entries_and_around_finds_original_conversation(
    permanent_entries,
):
    env, entries = permanent_entries
    for target, selected in (
        ({"person_id": env.person}, entries[:2]),
        ({"space_id": env.space}, entries[3:]),
    ):
        result = await env.service.execute(
            "query_chat_history",
            {"view": "search", "keyword": "雷达站", "target": target},
            env.context,
        )
        assert [row["event_id"] for row in result["events"]] == sorted(
            (entry.event.id for entry in selected), reverse=True
        )
    original = entries[1].event
    around = await env.service.execute(
        "query_chat_history",
        {"view": "around", "event_id": original.id, "before": 2, "after": 2},
        env.context,
    )
    assert [row["event_id"] for row in around["events"]] == [original.id]
    assert around["events"][0]["platform"] == "telegram"
    recent = await env.service.execute("query_chat_history", {"view": "recent"}, env.context)
    assert [row["content"] for row in recent["events"]] == ["hello"]
    assert env.bot.calls == []


async def test_global_search_keeps_real_delegated_conversation_scope(permanent_entries):
    env, entries = permanent_entries
    selected = entries[1]
    runtime = SimpleNamespace(
        read_target_id=env.person,
        read_scope=ConversationScope.private(
            selected.scope.bot_user_id,
            selected.scope.private_peer_user_id,
            platform=selected.scope.platform,
            conversation_id=selected.event.canonical_conversation_id,
        ),
    )
    token = current_invocation.set(ToolInvocationContext(runtime, call_id="delegated-search"))
    try:
        result = await env.service.execute(
            "query_chat_history", {"view": "search", "keyword": "雷达站"}, env.context
        )
        assert [row["event_id"] for row in result["events"]] == [selected.event.id]
        with pytest.raises(SocialError, match="history_scope_denied"):
            await env.service.execute(
                "query_chat_history",
                {
                    "view": "search",
                    "target": {"conversation_id": entries[-1].event.canonical_conversation_id},
                },
                env.context,
            )
    finally:
        current_invocation.reset(token)
