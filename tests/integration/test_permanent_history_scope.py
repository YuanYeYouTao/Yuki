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
            origin_key=thread,
            sender_user_id="10001",
            direction="inbound",
            content=f"雷达站 {thread}",
        )
        entries.append(SimpleNamespace(event=added.event, scope=scope))
    duplicate = await seed_scoped_event(
        env.service.writer,
        scope=entries[-1].scope,
        origin_key="duplicate-message",
        sender_user_id="10001",
        direction="inbound",
        content="雷达站 duplicate",
    )
    forgotten = await seed_scoped_event(
        env.service.writer,
        scope=entries[-1].scope,
        origin_key="forgotten-message",
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
    token = current_invocation.set(
        ToolInvocationContext(runtime, invocation_key="delegated-search")
    )
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


@pytest.mark.parametrize("view", ["recent", "search"])
@pytest.mark.parametrize(
    "source, selected",
    [
        ({"platform": "telegram"}, [1]),
        ({"scope": "private"}, [0, 1, 2]),
        ({"platform": "qq", "scope": "group"}, [3, 4]),
        ({"platform": "missing-platform"}, []),
        ({"platform": ""}, []),
    ],
)
async def test_source_filters_select_their_collection_once(
    permanent_entries, view, source, selected
):
    env, entries = permanent_entries
    result = await env.service.execute(
        "query_chat_history",
        {"view": view, "source": source, "keyword": "雷达站", "limit": 100},
        env.context,
    )
    assert [row["event_id"] for row in result["events"]] == sorted(
        (entries[index].event.id for index in selected), reverse=True
    )
    assert {row["conversation_id"] for row in result["sources"]} == {
        entries[index].event.canonical_conversation_id for index in selected
    }
    assert env.bot.calls == []


async def test_source_and_target_intersect_without_scope_fallback(permanent_entries):
    env, entries = permanent_entries
    target = {"conversation_id": entries[0].event.canonical_conversation_id}
    request = {"target": target, "source": {"platform": "telegram"}}
    for view in ("recent", "search", "around"):
        result = await env.service.execute(
            "query_chat_history",
            {**request, "view": view, "event_id": entries[1].event.id},
            env.context,
        )
        assert result["events"] == [] and result["sources"] == []
        assert result.source_event_ids == ()
        if view == "around":
            assert result["center_status"] == "unavailable"
    for source in ({}, {"platform": None, "scope": None}):
        result = await env.service.execute(
            "query_chat_history", {"view": "recent", "source": source}, env.context
        )
        assert [row["content"] for row in result["events"]] == ["hello"]


async def test_filtered_receipt_keeps_effect_and_actual_evidence(database, tmp_path):
    env = await social_env(database, tmp_path)
    receipt = await env.service.execute("send", {"text": "receipt proof"}, env.context)
    result = await env.service.execute(
        "query_chat_history",
        {
            "view": "search",
            "target": {"operation_id": receipt["operation_id"]},
            "source": {"platform": "telegram"},
        },
        env.context,
    )
    assert result["receipt_status"] == "succeeded"
    assert result["receipt_anchor"]["status"] == "unavailable"
    assert result["events"] == [] and result["sources"] == []
    assert result.source_event_ids == ()


@pytest.mark.parametrize("private", [False, True])
async def test_empty_entry_name_keeps_real_subject_name_discoverable(database, tmp_path, private):
    from yuki.identity.db_models import (
        AccountModel,
        CanonicalPersonModel,
        CanonicalSpaceModel,
        IdentityBindingModel,
    )

    env = await social_env(database, tmp_path)
    if private:
        added = await stored_private_entry(env, "telegram")
        conversation_id = added.event.canonical_conversation_id
        async with database.immediate_session() as session:
            binding = await session.get(IdentityBindingModel, added.binding_id)
            account = await session.get(AccountModel, binding.account_id)
            account.display_name = ""
            (await session.get(CanonicalPersonModel, env.person)).display_name = "Known archive"
    else:
        conversation_id = env.context.conversation_id
        async with database.immediate_session() as session:
            (await session.get(CanonicalSpaceModel, env.space)).name = "Known archive"
            binding = await session.scalar(
                select(SpaceBindingModel).where(SpaceBindingModel.space_id == env.space)
            )
            binding.display_name = ""
    target = {"conversation_id": conversation_id}
    directory = await env.service.execute(
        "find_conversations",
        {"target": target, "display_name": "Known archive"},
        env.context,
    )
    history = await env.service.execute("query_chat_history", {"target": target}, env.context)
    assert [row["conversation_id"] for row in directory["items"]] == [conversation_id]
    assert history["events"][0]["conversation_name"] == "Known archive"
