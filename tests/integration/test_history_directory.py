"""find_contacts pages subjects with real entry facts, without inheriting sendability gates."""

from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError
from sqlalchemy import select, text
from tests.support.canonical_identity import ensure_person, ensure_space
from tests.support.event_seed import seed_scoped_event
from tests.support.social_history_cases import stored_private_entry
from tests.support.social_identity_cases import social_env

from yuki.capabilities.invocation import ToolInvocationContext, current_invocation
from yuki.conversation.hydrate import ensure_canonical_conversation
from yuki.domain.conversations import ConversationScope
from yuki.identity.canonical_repository import create_space_binding, replace_assignment
from yuki.identity.db_models import (
    CanonicalPersonModel,
    IdentityBindingModel,
    PresenceModel,
    SpaceBindingModel,
)
from yuki.identity.person_identity import merge_persons, prepare_person_merge
from yuki.services.agent_tools import ToolRuntime
from yuki.services.contact_query import ContactsQuery
from yuki.social.agent_adapter import invoke_social
from yuki.social.models import SocialError
from yuki.social.tools import social_tool_definitions


def test_find_contacts_schema_and_query_reject_old_selectors_and_two_key_targets():
    (schema,) = [
        tool.parameters for tool in social_tool_definitions() if tool.name == "find_contacts"
    ]
    validator = Draft202012Validator(schema)
    validator.validate({"name": "群" * 128, "type": "group", "platform": "telegram"})
    for value in (
        {"kind": "space"},
        {"display_name": "x"},
        {"subject_ref": "current_speaker"},
        {"source": {"platform": "telegram"}},
        {"target": {}},
        {"target": {"person_id": "a", "space_id": "b"}},
        {"target": {"operation_id": "00000000-0000-0000-0000-000000000001"}},
        {"limit": 0},
    ):
        assert list(validator.iter_errors(value)), value
    for value in (
        {"kind": "space"},
        {"target": {}},
        {"target": {"operation_id": "00000000-0000-0000-0000-000000000001"}},
        {"cursor": "not-a-cursor"},
        {"limit": 0},
    ):
        with pytest.raises(ValidationError):
            ContactsQuery.model_validate(value)


async def test_group_name_and_platform_filter_precede_paging_and_all_pages_are_reachable(
    database, tmp_path
):
    env = await social_env(database, tmp_path)
    env.bot.calls.clear()
    expected = set()
    now = datetime.now(UTC)
    async with database.immediate_session() as session:
        for index in range(48):
            platform = "telegram" if index % 2 else "qq"
            binding = await create_space_binding(
                session,
                external_space_id=f"directory-{index}",
                name="同名群",
                enabled=True,
                autonomous_enabled=True,
                require_mention=True,
                now=now,
                platform=platform,
            )
            if platform == "telegram":
                expected.add(binding.space_id)
    seen = []
    arguments = {"type": "group", "name": "同名群", "platform": "telegram", "limit": 7}
    while True:
        page = await env.service.execute("find_contacts", arguments, env.context)
        identities = [item["id"] for item in page["items"]]
        assert identities == sorted(identities)
        assert all(item["type"] == "group" for item in page["items"])
        assert all(
            entry["platform"] == "telegram" for item in page["items"] for entry in item["entries"]
        )
        seen.extend(identities)
        if "next_cursor" not in page:
            break
        arguments["cursor"] = page["next_cursor"]
    assert seen == sorted(expected)
    whole = await env.service.execute(
        "find_contacts", {**arguments, "cursor": None, "limit": 30}, env.context
    )
    assert len(whole["items"]) == 24 and "next_cursor" not in whole
    long_name = await env.service.execute(
        "find_contacts", {"type": "group", "name": "群" * 128}, env.context
    )
    assert long_name["items"] == []
    assert env.bot.calls == []


async def test_people_then_groups_share_one_cursor(database, tmp_path):
    env = await social_env(database, tmp_path)
    people = {}
    async with database.immediate_session() as session:
        for index in range(5):
            account = str(912300000 + index)
            people[account] = await ensure_person(session, account, display_name="分页对象")
        group = await ensure_space(session, "paging-group", name="分页对象")
    for account in people:
        await seed_scoped_event(
            env.service.writer,
            scope=ConversationScope.group("80001", "20001"),
            origin_key=f"person-directory-{account}",
            sender_user_id=account,
            direction="inbound",
            content="observed person",
        )
    arguments = {"name": "分页对象", "limit": 2}
    seen = []
    while True:
        page = await env.service.execute("find_contacts", arguments, env.context)
        seen.extend((item["type"], item["id"]) for item in page["items"])
        if "next_cursor" not in page:
            break
        arguments["cursor"] = page["next_cursor"]
    assert seen == [("person", identity) for identity in sorted(people.values())] + [
        ("group", group)
    ]


async def test_explicit_target_keeps_ended_binding_disabled_presence_and_disabled_person(
    database, tmp_path
):
    env = await social_env(database, tmp_path)
    env.bot.calls.clear()
    entry = await stored_private_entry(env, "telegram")
    async with database.immediate_session() as session:
        binding = await session.get(IdentityBindingModel, entry.binding_id)
        presence = await session.get(PresenceModel, entry.scope.presence_id)
        presence.status = "disabled"
        person = await session.get(CanonicalPersonModel, env.person)
        person.enabled = False
        await replace_assignment(session, binding.account, binding.id, None, now=datetime.now(UTC))
    env.registry.disconnect(env.bot)
    # The actor itself stays enabled; only the looked-up person is disabled.
    context = replace(env.context, actor=None)
    for target in (
        {"binding_id": entry.binding_id},
        {"conversation_id": entry.event.canonical_conversation_id},
        {"person_id": env.person},
    ):
        result = await env.service.execute(
            "find_contacts", {"target": target, "platform": "telegram"}, context
        )
        (item,) = result["items"]
        assert item["type"] == "person" and item["id"] == env.person
        (conversation,) = item["conversations"]
        assert conversation == {
            "conversation_id": entry.event.canonical_conversation_id,
            "platform": "telegram",
            "scope": "private",
            "name": "telegram friend",
            "binding_status": "ended",
            "presence_status": "disabled",
        }
    assert env.bot.calls == []


async def test_group_conversations_flatten_threads_and_keep_legacy_unknown_platform(
    database, tmp_path
):
    env = await social_env(database, tmp_path)
    async with database.immediate_session() as session:
        space = await ensure_space(session, "legacy-directory", name="原群")
        legacy = await ensure_canonical_conversation(
            session, kind="space", primary_scope_key=f"space:{space}", space_id=space
        )
        binding = await session.scalar(
            select(SpaceBindingModel).where(SpaceBindingModel.space_id == space)
        )
        conversations = {legacy.conversation_id}
        for index in range(23):
            entry = await ensure_canonical_conversation(
                session,
                kind="space",
                primary_scope_key=f"directory-thread:{index}",
                space_id=space,
                space_binding_id=binding.id,
                thread_id=str(index),
            )
            conversations.add(entry.conversation_id)
    (item,) = (
        await env.service.execute("find_contacts", {"target": {"space_id": space}}, env.context)
    )["items"]
    assert item["type"] == "group" and item["name"] == "原群"
    assert {row["conversation_id"] for row in item["conversations"]} == conversations
    for row in item["conversations"]:
        if row["conversation_id"] == legacy.conversation_id:
            assert "platform" not in row and "thread_id" not in row
        else:
            assert row["platform"] == "qq" and row["thread_id"]
    (qq,) = (
        await env.service.execute(
            "find_contacts", {"target": {"space_id": space}, "platform": "qq"}, env.context
        )
    )["items"]
    assert legacy.conversation_id not in {row["conversation_id"] for row in qq["conversations"]}


async def test_directory_keeps_real_read_delegation(database, tmp_path):
    env = await social_env(database, tmp_path)
    private = await stored_private_entry(env, "telegram")
    runtime = ToolRuntime(
        inbound=None,
        conversation_id=env.context.conversation_id,
        execution_id="directory-grant",
        read_target_id=env.space,
        read_scope=ConversationScope.group(
            "80001", "20001", conversation_id=env.context.conversation_id
        ),
    )
    token = current_invocation.set(ToolInvocationContext(runtime, invocation_key="directory-grant"))
    try:
        page = await invoke_social(env.service, "find_contacts", {}, runtime)
        (item,) = page["items"]
        assert item["id"] == env.space and item["conversations"]
        with pytest.raises(SocialError, match="history_scope_denied"):
            await invoke_social(
                env.service,
                "find_contacts",
                {"target": {"conversation_id": private.event.canonical_conversation_id}},
                runtime,
            )
        with pytest.raises(SocialError, match="history_scope_denied"):
            await invoke_social(env.service, "find_contacts", {"type": "person"}, runtime)
    finally:
        current_invocation.reset(token)


async def test_person_family_keeps_both_entries_and_filters_original_platform(database, tmp_path):
    env = await social_env(database, tmp_path)
    first = await stored_private_entry(env, "telegram")
    async with database.immediate_session() as session:
        root = await ensure_person(session, "new-person")
    second = await stored_private_entry(env, "feishu", person_id=root)
    async with database.read_session() as reader:
        await reader.execute(text("BEGIN"))
        prepared = await prepare_person_merge(reader, env.person, root)
    async with database.immediate_session() as session:
        await merge_persons(session, env.person, root, prepared=prepared)
    for person_id in (env.person, root):
        (item,) = (
            await env.service.execute(
                "find_contacts", {"target": {"person_id": person_id}}, env.context
            )
        )["items"]
        assert item["id"] == root
        assert {row["conversation_id"] for row in item["conversations"]} == {
            first.event.canonical_conversation_id,
            second.event.canonical_conversation_id,
        }
    (filtered,) = (
        await env.service.execute(
            "find_contacts", {"target": {"person_id": root}, "platform": "telegram"}, env.context
        )
    )["items"]
    assert [row["conversation_id"] for row in filtered["conversations"]] == [
        first.event.canonical_conversation_id
    ]


async def test_disabled_actor_cannot_read_the_directory(database, tmp_path):
    env = await social_env(database, tmp_path)
    async with database.immediate_session() as session:
        (await session.get(CanonicalPersonModel, env.person)).enabled = False
    with pytest.raises(SocialError, match="permission_denied"):
        await env.service.execute(
            "find_contacts",
            {},
            replace(
                env.context, actor=SimpleNamespace(person_id=env.person, principal_kind="person")
            ),
        )
