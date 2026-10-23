"""Native mention routing keeps Person identity and platform account facts separate."""

import json
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select
from tests.support.canonical_identity import ensure_person
from tests.support.commands_and_chat_helpers import inbound
from tests.support.event_seed import seed_scoped_event
from tests.support.social_identity_cases import add_second_account, social_env

from yuki.capabilities.invocation import ToolInvocationContext, current_invocation
from yuki.domain.conversations import ConversationScope
from yuki.gateway.providers.telegram import TelegramClient
from yuki.identity.canonical_repository import (
    create_person_binding,
    create_presence,
    create_space_binding,
)
from yuki.identity.db_models import AccountModel, CanonicalPersonModel, IdentityBindingModel
from yuki.persistence.models import ChatEventModel
from yuki.services.agent_tools import ToolRuntime
from yuki.social.agent_adapter import invoke_social


async def invoke(env, arguments, *, account="10001"):
    if account != "10001":
        await seed_scoped_event(
            env.service.writer,
            scope=ConversationScope.group(env.bot.self_id, "20001"),
            platform_message_id="second-account-input",
            sender_user_id=account,
            direction="inbound",
            content="hello from my other account",
        )
    async with env.db.read_session() as session:
        event = await session.scalar(
            select(ChatEventModel).where(
                ChatEventModel.direction == "inbound", ChatEventModel.sender_user_id == account
            )
        )
    message = replace(
        inbound("test", message_id=event.platform_message_id, user_id=account, group_id="20001"),
        bot_user_id=env.bot.self_id,
        source_event_id=event.id,
        conversation_id=event.canonical_conversation_id,
        person_id=env.person,
        space_id=env.space,
        presence_id=env.presence,
    )
    runtime = ToolRuntime(inbound=message)
    token = current_invocation.set(ToolInvocationContext(runtime=runtime, call_id="mention"))
    try:
        return await invoke_social(env.service, "send", arguments, runtime)
    finally:
        current_invocation.reset(token)


async def telegram_target(env, accounts):
    wire = []

    def handle(request):
        method = request.url.path.rsplit("/", 1)[-1]
        params = json.loads(request.content)
        wire.append((method, params))
        if method == "getChatMember":
            result = {"status": "member"}
        elif method == "sendMessage":
            result = {"message_id": len(wire), "chat": {"id": params["chat_id"]}}
        else:
            raise AssertionError(method)
        return httpx.Response(200, json={"ok": True, "result": result})

    transport = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    client = TelegramClient("offline-fixture-token", client=transport)
    client.self_id = "80002"
    now = datetime.now(UTC)
    async with env.db.immediate_session() as session:
        presence = await create_presence(
            session, external_account_id=client.self_id, now=now, platform="telegram"
        )
        group = await create_space_binding(
            session,
            external_space_id="-10042",
            name="Telegram target",
            enabled=True,
            autonomous_enabled=False,
            require_mention=False,
            now=now,
            platform="telegram",
        )
        bindings = []
        for account in accounts:
            binding = await create_person_binding(
                session,
                external_account_id=account,
                display_name="same person on another platform",
                now=now,
                platform="telegram",
            )
            from tests.support.person_merge import merge_persons

            await merge_persons(session, binding.person_id, env.person)
            bindings.append(binding.id)
    env.registry.connect(client, provider_id="telegram", presence_id=presence.id)
    assert await env.router.cas_takeover_space(group.space_id) == "taken"
    wire.clear()
    return SimpleNamespace(transport=transport, wire=wire, space=group.space_id, bindings=bindings)


async def account_id(env, native, platform="qq"):
    async with env.db.read_session() as session:
        return await session.scalar(
            select(AccountModel.id).where(
                AccountModel.platform == platform, AccountModel.external_account_id == native
            )
        )


async def observe(env, binding_id, name, *, group="20001"):
    from tests.support.account_observations import apply_observations, profile_message

    async with env.db.read_session() as session:
        binding = await session.get(IdentityBindingModel, binding_id)
    await apply_observations(
        env.db,
        profile_message(
            user_id=binding.external_account_id,
            nickname=name,
            group_id=group,
            group_card=name,
            platform=binding.platform,
        ),
    )


@pytest.mark.parametrize("account", ["10001", "10002"])
async def test_same_person_exact_account_does_not_follow_current_speaker(
    database, tmp_path, account
):
    env = await social_env(database, tmp_path)
    await add_second_account(env)
    selected = await account_id(env, account)
    result = await invoke(env, {"text": f"前@{{account:{selected}}}后"})
    assert result["status"] == "succeeded"
    sent = [params for method, params in env.bot.calls if method == "send_group_msg"]
    assert sent[-1]["message"][1] == {"type": "at", "data": {"qq": account}}


@pytest.mark.parametrize("native", ["10001", "10003"])
async def test_cross_platform_exact_account_uses_target_namespace(database, tmp_path, native):
    env = await social_env(database, tmp_path)
    target = await telegram_target(env, [native])
    try:
        selected = await account_id(env, native, "telegram")
        result = await invoke(
            env,
            {
                "target": {"kind": "space", "target_id": target.space},
                "text": f"@{{account:{selected}}} hello",
            },
        )
        assert result["status"] == "succeeded"
        assert [item["account_id"] for item in result["mentioned_accounts"]] == [selected]
        sent = [params for method, params in target.wire if method == "sendMessage"]
        assert len(sent) == 1 and f"tg://user?id={native}" in sent[0]["text"]
        assert not any(method == "send_group_msg" for method, _ in env.bot.calls)
    finally:
        await target.transport.aclose()


async def test_unknown_telegram_username_does_not_invent_an_internal_account(database, tmp_path):
    env = await social_env(database, tmp_path)
    target = await telegram_target(env, [])
    try:
        result = await invoke(
            env,
            {
                "target": {"kind": "space", "target_id": target.space},
                "text": "请到 @Remote_User 看看",
            },
        )
        assert result["status"] == "succeeded" and "mentioned_accounts" not in result
        sent = [params for method, params in target.wire if method == "sendMessage"]
        assert len(sent) == 1 and "@Remote_User" in sent[0]["text"]
    finally:
        await target.transport.aclose()


async def test_same_person_two_accounts_default_to_the_recent_speaking_account(database, tmp_path):
    env = await social_env(database, tmp_path)
    other = await add_second_account(env)
    async with env.db.read_session() as session:
        first = await session.scalar(
            select(IdentityBindingModel).where(IdentityBindingModel.account_id == env.account)
        )
    await observe(env, first.id, "同名")
    await observe(env, other, "同名")
    result = await invoke(env, {"text": "@同名看看"})
    assert result["status"] == "succeeded" and not result.get("unresolved_mentions")
    sent = env.bot.calls[-1][1]["message"]
    assert sent == [
        {"type": "at", "data": {"qq": "10001"}},
        {"type": "text", "data": {"text": "看看"}},
    ]
    assert result["mentioned_accounts"] == [
        {
            "account_id": env.account,
            "display_name": "同名",
            "reference": f"@{{account:{env.account}}}",
        }
    ]


async def test_target_with_no_inbound_history_and_disabled_person_can_be_mentioned(
    database, tmp_path
):
    env = await social_env(database, tmp_path)
    async with env.db.immediate_session() as session:
        await ensure_person(session, "10003")
        target = await session.scalar(
            select(IdentityBindingModel)
            .join(AccountModel)
            .where(AccountModel.platform == "qq", AccountModel.external_account_id == "10003")
        )
        person = await session.get(CanonicalPersonModel, target.person_id)
        person.enabled = False
        target.enabled = False
        target_id = target.id
    await observe(env, target_id, "未发言成员")
    env.bot.missing.add("10003")
    env.bot.calls.clear()
    result = await invoke(env, {"text": "@未发言成员你好"})
    assert result["status"] == "succeeded"
    assert env.bot.calls == [
        (
            "send_group_msg",
            {
                "group_id": 20001,
                "message": [
                    {"type": "at", "data": {"qq": "10003"}},
                    {"type": "text", "data": {"text": "你好"}},
                ],
            },
        )
    ]


async def test_exact_account_from_other_platform_remains_literal_with_resolution_feedback(
    database, tmp_path
):
    env = await social_env(database, tmp_path)
    target = await telegram_target(env, ["10003"])
    try:
        result = await invoke(
            env,
            {
                "target": {"kind": "space", "target_id": target.space},
                "text": f"@{{account:{env.account}}}",
            },
        )
        assert result["status"] == "succeeded" and result["unresolved_mentions"]
        sent = [params for method, params in target.wire if method == "sendMessage"]
        assert len(sent) == 1 and "tg://user" not in sent[0]["text"]
    finally:
        await target.transport.aclose()
