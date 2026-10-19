"""Native mention routing keeps Person identity and platform account facts separate."""

import json
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select
from tests.support.commands_and_chat_helpers import inbound
from tests.support.social_identity_cases import add_second_account, social_env

from yuki.capabilities.invocation import ToolInvocationContext, current_invocation
from yuki.domain.conversations import ConversationScope
from yuki.gateway.providers.telegram import TelegramClient
from yuki.identity.canonical_repository import (
    create_person_binding,
    create_presence,
    create_space_binding,
)
from yuki.identity.db_models import IdentityBindingModel
from yuki.persistence.models import ChatEventModel
from yuki.services.agent_tools import ToolRuntime
from yuki.social.agent_adapter import invoke_social
from yuki.social.models import SocialError


async def invoke(env, arguments, *, account="10001"):
    if account != "10001":
        await env.service.writer.append(
            scope=ConversationScope.group(env.bot.self_id, "20001"),
            platform_message_id="second-account-input",
            sender_user_id=account,
            direction="inbound",
            content="hello from my other account",
        )
    async with env.db.sessions() as session:
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
        return await invoke_social(env.service, "send_message", arguments, runtime)
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
            binding.person_id = env.person
            bindings.append(binding.id)
    env.registry.connect(client, provider_id="telegram", presence_id=presence.id)
    assert await env.router.cas_takeover_space(group.space_id) == "taken"
    wire.clear()
    return SimpleNamespace(transport=transport, wire=wire, space=group.space_id, bindings=bindings)


@pytest.mark.parametrize("account", ["10001", "10002"])
async def test_same_platform_current_speaker_keeps_real_inbound_account(
    database, tmp_path, account
):
    env = await social_env(database, tmp_path)
    await add_second_account(env)
    result = await invoke(
        env, {"text": "hello", "mentions": [{"subject_ref": "current_speaker"}]}, account=account
    )
    assert result["status"] == "succeeded"
    sends = [params for method, params in env.bot.calls if method == "send_group_msg"]
    assert len(sends) == 1
    assert sends[0]["message"][0] == {"type": "at", "data": {"qq": account}}


async def test_same_platform_explicit_binding_overrides_inbound_account(database, tmp_path):
    env = await social_env(database, tmp_path)
    other = await add_second_account(env)
    result = await invoke(
        env,
        {
            "text": "hello",
            "mentions": [{"subject_ref": "current_speaker", "binding_id": other}],
        },
    )
    assert result["status"] == "succeeded"
    sends = [params for method, params in env.bot.calls if method == "send_group_msg"]
    assert len(sends) == 1
    assert sends[0]["message"][0] == {"type": "at", "data": {"qq": "10002"}}


@pytest.mark.parametrize("account", ["10001", "10003"])
async def test_cross_platform_current_speaker_uses_target_person_binding(
    database, tmp_path, account
):
    env = await social_env(database, tmp_path)
    target = await telegram_target(env, [account])
    try:
        result = await invoke(
            env,
            {
                "target": {"kind": "space", "target_id": target.space},
                "text": "hello",
                "mentions": [{"subject_ref": "current_speaker"}],
            },
        )
        assert result["status"] == "succeeded"
        sent = [params for method, params in target.wire if method == "sendMessage"]
        assert len(sent) == 1 and f"tg://user?id={account}" in sent[0]["text"]
        assert not any(method == "send_group_msg" for method, _ in env.bot.calls)
    finally:
        await target.transport.aclose()


async def test_equal_external_number_does_not_resolve_cross_platform_ambiguity(database, tmp_path):
    env = await social_env(database, tmp_path)
    target = await telegram_target(env, ["10001", "10003"])
    try:
        with pytest.raises(SocialError, match="binding_ambiguous"):
            await invoke(
                env,
                {
                    "target": {"kind": "space", "target_id": target.space},
                    "text": "hello",
                    "mentions": [{"subject_ref": "current_speaker"}],
                },
            )
        assert not any(method == "sendMessage" for method, _ in target.wire)
    finally:
        await target.transport.aclose()


async def test_explicit_binding_resolves_cross_platform_mention(database, tmp_path):
    env = await social_env(database, tmp_path)
    target = await telegram_target(env, ["10001", "10003"])
    try:
        result = await invoke(
            env,
            {
                "target": {"kind": "space", "target_id": target.space},
                "text": "hello",
                "mentions": [{"subject_ref": "current_speaker", "binding_id": target.bindings[1]}],
            },
        )
        assert result["status"] == "succeeded"
        sent = [params for method, params in target.wire if method == "sendMessage"]
        assert len(sent) == 1 and "tg://user?id=10003" in sent[0]["text"]
    finally:
        await target.transport.aclose()


async def test_mention_rejects_explicit_binding_from_another_platform(database, tmp_path):
    env = await social_env(database, tmp_path)
    target = await telegram_target(env, ["10003"])
    async with env.db.sessions() as session:
        source_binding = await session.scalar(
            select(IdentityBindingModel.id).where(
                IdentityBindingModel.person_id == env.person,
                IdentityBindingModel.platform == "qq",
            )
        )
    try:
        with pytest.raises(SocialError, match="binding_unavailable"):
            await invoke(
                env,
                {
                    "target": {"kind": "space", "target_id": target.space},
                    "text": "hello",
                    "mentions": [{"subject_ref": "current_speaker", "binding_id": source_binding}],
                },
            )
        assert not any(method == "sendMessage" for method, _ in target.wire)
    finally:
        await target.transport.aclose()
