"""Directory pages expose real entry facts without inheriting sendability gates."""

from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError
from sqlalchemy import delete, select
from tests.support.canonical_identity import ensure_person, ensure_space
from tests.support.event_seed import seed_scoped_event
from tests.support.social_history_cases import stored_private_entry
from tests.support.social_identity_cases import social_env

from yuki.capabilities.invocation import ToolInvocationContext, current_invocation
from yuki.conversation.canonical_db_models import (
    CanonicalConversationModel,
    ConversationLegacyAliasModel,
)
from yuki.conversation.hydrate import ensure_canonical_conversation
from yuki.domain.conversations import ConversationScope
from yuki.identity.canonical_repository import create_space_binding, replace_assignment
from yuki.identity.db_models import (
    CanonicalPersonModel,
    IdentityBindingModel,
    PresenceModel,
    SpaceBindingModel,
)
from yuki.identity.person_identity import merge_persons
from yuki.services.agent_tools import ToolRuntime
from yuki.services.contact_query import CONTACT_QUERY, contact_schema
from yuki.social.agent_adapter import invoke_social
from yuki.social.models import SocialError


def test_contact_views_share_one_schema_without_name_cap_or_global_kind_requirement():
    schema = contact_schema()
    assert "discriminator" not in schema
    validator = Draft202012Validator(schema)
    validator.validate({"kind": "space", "display_name": "群" * 200})
    validator.validate({"view": "entries", "source": {"platform": "telegram"}})
    for value in (
        {"view": "subjects"},
        {"view": "entries", "kind": "space"},
        {"view": "entries", "subject_ref": "current_speaker"},
        {"view": "subjects", "kind": "space", "limit": 0},
    ):
        with pytest.raises(ValidationError):
            CONTACT_QUERY.validate_python(value)
        assert list(validator.iter_errors(value))


async def test_subject_source_filter_precedes_paging_and_all_pages_are_reachable(
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
    arguments = {
        "kind": "space",
        "display_name": "同名群",
        "source": {"platform": "telegram", "scope": "group"},
        "limit": 7,
    }
    while True:
        page = await env.service.execute("find_contacts", arguments, env.context)
        identities = [item["target_id"] for item in page["items"]]
        assert identities == sorted(identities)
        assert all(
            binding["platform"] == "telegram"
            for item in page["items"]
            for binding in item["bindings"]
        )
        seen.extend(identities)
        if not page["has_more"]:
            assert page["next_cursor"] is None
            break
        arguments["cursor"] = page["next_cursor"]
    assert seen == sorted(expected)
    whole = await env.service.execute(
        "find_contacts", {**arguments, "cursor": None, "limit": 30}, env.context
    )
    assert len(whole["items"]) == 24 and not whole["has_more"]
    mismatch = await env.service.execute(
        "find_contacts", {"kind": "space", "source": {"scope": "private"}}, env.context
    )
    assert mismatch["items"] == []
    long_name = await env.service.execute(
        "find_contacts", {"kind": "space", "display_name": "群" * 200}, env.context
    )
    assert long_name["items"] == []
    assert env.bot.calls == []


async def test_more_than_twenty_people_are_reachable_through_subject_name_pages(database, tmp_path):
    env = await social_env(database, tmp_path)
    people = {}
    async with database.immediate_session() as session:
        for index in range(24):
            account = str(912300000 + index)
            people[account] = await ensure_person(session, account, display_name="分页人物")
    for account in people:
        await seed_scoped_event(
            env.service.writer,
            scope=ConversationScope.group("80001", "20001"),
            platform_message_id=f"person-directory-{account}",
            sender_user_id=account,
            direction="inbound",
            content="observed person",
        )
    arguments = {"kind": "person", "display_name": "分页人物", "limit": 9}
    seen = []
    while True:
        page = await env.service.execute("find_contacts", arguments, env.context)
        seen.extend(item["target_id"] for item in page["items"])
        if not page["has_more"]:
            assert page["next_cursor"] is None
            break
        arguments["cursor"] = page["next_cursor"]
    assert seen == sorted(people.values())


async def test_entries_keep_ended_binding_and_original_disabled_presence(database, tmp_path):
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
    result = await env.service.execute(
        "find_contacts",
        {
            "view": "entries",
            "target": {"binding_id": entry.binding_id},
            "source": {"platform": "telegram", "scope": "private"},
        },
        env.context,
    )
    assert len(result["items"]) == 1
    actual = result["items"][0]
    assert actual["conversation_id"] == entry.event.canonical_conversation_id
    assert actual["binding_id"] == entry.binding_id
    assert actual["entry_presence_id"] == entry.scope.presence_id
    assert actual["binding_status"] == "ended"
    assert actual["presence_status"] == "disabled"
    assert actual["target_id"] == env.person
    assert env.bot.calls == []


async def test_entries_flatten_threads_and_keep_legacy_unknown_platform(database, tmp_path):
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
    seen = []
    deleted_cursor = None
    arguments = {"view": "entries", "target": {"space_id": space}, "limit": 6}
    while True:
        page = await env.service.execute("find_contacts", arguments, env.context)
        seen.extend(item["conversation_id"] for item in page["items"])
        for item in page["items"]:
            if item["conversation_id"] == legacy.conversation_id:
                assert item["platform"] is None and item["binding_id"] is None
            else:
                assert item["platform"] == "qq" and item["binding_id"] == binding.id
        if not page["has_more"]:
            break
        if deleted_cursor is None:
            deleted_cursor = page["next_cursor"]
            async with database.immediate_session() as session:
                await session.execute(
                    delete(CanonicalConversationModel).where(
                        CanonicalConversationModel.id == deleted_cursor
                    )
                )
                await session.execute(
                    delete(ConversationLegacyAliasModel).where(
                        ConversationLegacyAliasModel.conversation_id == deleted_cursor
                    )
                )
            async with database.sessions() as session:
                assert await session.get(CanonicalConversationModel, deleted_cursor) is None
        arguments["cursor"] = page["next_cursor"]
    assert seen == sorted(conversations)
    named = await env.service.execute(
        "find_contacts",
        {**arguments, "cursor": None, "display_name": "原群", "limit": 30},
        env.context,
    )
    assert len(named["items"]) == 23
    assert deleted_cursor not in {item["conversation_id"] for item in named["items"]}
    actual_platform = await env.service.execute(
        "find_contacts", {**arguments, "cursor": None, "source": {"platform": "qq"}}, env.context
    )
    assert actual_platform["items"] and all(
        item["conversation_id"] != legacy.conversation_id for item in actual_platform["items"]
    )


async def test_entry_directory_keeps_real_read_delegation(database, tmp_path):
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
    token = current_invocation.set(ToolInvocationContext(runtime, call_id="directory-grant"))
    try:
        page = await invoke_social(env.service, "find_contacts", {"view": "entries"}, runtime)
        assert page["items"] and all(item["space_id"] == env.space for item in page["items"])
        other = await invoke_social(
            env.service,
            "find_contacts",
            {
                "view": "entries",
                "target": {"conversation_id": private.event.canonical_conversation_id},
            },
            runtime,
        )
        assert other["items"] == []
    finally:
        current_invocation.reset(token)


async def test_entry_discovery_retains_person_family_and_filters_original_platform(
    database, tmp_path
):
    env = await social_env(database, tmp_path)
    first = await stored_private_entry(env, "telegram")
    async with database.immediate_session() as session:
        root = await ensure_person(session, "new-person")
    second = await stored_private_entry(env, "feishu", person_id=root)
    async with database.immediate_session() as session:
        await merge_persons(session, env.person, root)
    for person_id in (env.person, root):
        result = await env.service.execute(
            "find_contacts", {"view": "entries", "target": {"person_id": person_id}}, env.context
        )
        assert {item["conversation_id"] for item in result["items"]} == {
            first.event.canonical_conversation_id,
            second.event.canonical_conversation_id,
        }
        assert {item["person_id"] for item in result["items"]} == {env.person, root}
    filtered = await env.service.execute(
        "find_contacts",
        {
            "view": "entries",
            "target": {"person_id": root},
            "source": {"platform": "telegram"},
        },
        env.context,
    )
    assert [item["conversation_id"] for item in filtered["items"]] == [
        first.event.canonical_conversation_id
    ]


async def test_entries_keep_original_operation_source_and_current_actor_revocation(
    database, tmp_path
):
    env = await social_env(database, tmp_path)
    receipt = await env.service.execute("send_message", {"text": "saved receipt"}, env.context)
    request = {"view": "entries", "target": {"operation_id": receipt["operation_id"]}}
    page = await env.service.execute("find_contacts", request, env.context)
    assert [item["conversation_id"] for item in page["items"]] == [env.context.conversation_id]
    source_conflict = await env.service.execute(
        "find_contacts", {**request, "source": {"platform": "telegram"}}, env.context
    )
    assert source_conflict["items"] == []
    private = await stored_private_entry(env, "telegram")
    with pytest.raises(SocialError, match="history_receipt_unavailable"):
        await env.service.execute(
            "find_contacts",
            request,
            replace(env.context, conversation_id=private.event.canonical_conversation_id),
        )
    async with database.immediate_session() as session:
        (await session.get(CanonicalPersonModel, env.person)).enabled = False
    with pytest.raises(SocialError, match="permission_denied"):
        await env.service.execute(
            "find_contacts",
            {"view": "entries"},
            replace(
                env.context, actor=SimpleNamespace(person_id=env.person, principal_kind="person")
            ),
        )
