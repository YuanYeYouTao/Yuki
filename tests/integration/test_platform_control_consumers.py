"""Native control commands and admitted consumers share canonical identity."""

import re
from datetime import UTC
from html import unescape
from math import ceil
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from tests.conftest import MemorySender
from tests.integration.test_telegram_runtime import drain, update
from tests.integration.test_telegram_runtime import telegram_env as telegram_env
from tests.unit.test_app_messages import admin as admin
from tests.unit.test_app_messages import app_env as app_env
from tests.unit.test_app_messages import drain as drain_app
from tests.unit.test_canonical_ingress import _message
from tests.unit.test_client_api import bearer, pair, person_and_code
from tests.unit.test_client_identity import code_for
from tests.unit.test_client_link import stack

from yuki.adapters.onebot.sender import OneBotSender
from yuki.client_api.db_models import ClientLinkCodeModel
from yuki.domain.conversations import ScopeType
from yuki.domain.messages import InboundMessage, SenderIdentity
from yuki.gateway.providers.telegram_ingress import TelegramSender
from yuki.identity.canonical_repository import find_identity_binding
from yuki.persistence.models import ChatEventModel
from yuki.services.client_link import ClientLinkCommands
from yuki.services.rate_limit import SlidingWindowRateLimiter


async def test_telegram_link_consumes_code_without_chat_or_model(telegram_env, database):
    env = telegram_env
    service, target, issued = await code_for(database)
    env.harness.processor._client_links = ClientLinkCommands(service, env.gateway.registry)
    await env.gateway.receive(update(text=f"/ai link {issued['code']}"))
    await drain(env)
    async with database.sessions() as reader:
        binding = await find_identity_binding(reader, "42", platform="telegram")
        code = await reader.scalar(select(ClientLinkCodeModel))
        rows = list(await reader.scalars(select(ChatEventModel)))
    assert binding.person_id == target
    assert code.used_at is not None
    assert not rows
    assert not env.model.requests

    await env.gateway.receive(update(2, text=f"/ai link {issued['code']}"))
    await drain(env)
    assert not env.model.requests
    async with database.sessions() as reader:
        assert not list(await reader.scalars(select(ChatEventModel)))
        assert (await find_identity_binding(reader, "42", platform="telegram")).person_id == target


async def test_app_link_text_stays_in_personal_chat(app_env):
    env = app_env
    env.harness.processor._client_links = ClientLinkCommands(env.identity, env.gateway.registry)
    _, code = await person_and_code(env.client)
    device = await pair(env.client, code)
    response = await env.client.post(
        "/client/v1/messages",
        json={"request_id": str(uuid4()), "text": "/ai link hello"},
        headers=bearer(device["token"]),
    )
    assert response.status_code == 200
    await drain_app(env)
    assert len(env.calls) == 2


async def test_invalid_native_link_never_creates_chat_or_person(telegram_env, database):
    env = telegram_env
    service, _, _ = await code_for(database)
    env.harness.processor._client_links = ClientLinkCommands(service, env.gateway.registry)
    await env.gateway.receive(update(text="/ai link invalid"))
    await drain(env)
    assert not env.model.requests
    async with database.sessions() as reader:
        assert not list(await reader.scalars(select(ChatEventModel)))
        assert await find_identity_binding(reader, "42", platform="telegram") is None


async def test_confirmed_reply_keeps_original_owner_during_binding_transfer(
    telegram_env, database, monkeypatch
):
    env = telegram_env
    service, target, _ = await code_for(database)
    original_send = TelegramSender.send

    async def send_then_transfer(sender, message):
        receipt = await original_send(sender, message)
        async with database.sessions() as reader:
            binding = await find_identity_binding(reader, "42", platform="telegram")
        await service.transfer_for_operator(binding.id, target, str(uuid4()), str(uuid4()))
        return receipt

    monkeypatch.setattr(TelegramSender, "send", send_then_transfer)
    await env.gateway.receive(update(text="/ai ping"))
    await drain(env)
    async with database.sessions() as reader:
        rows = list(await reader.scalars(select(ChatEventModel).order_by(ChatEventModel.id)))
        binding = await find_identity_binding(reader, "42", platform="telegram")
    assert len(rows) == 2
    assert rows[0].canonical_conversation_id == rows[1].canonical_conversation_id
    assert rows[0].author_person_id != binding.person_id == target
    assert rows[1].author_presence_id == env.gateway.presence_id
    assert not env.model.requests


class _QQBot:
    self_id = "8000"

    async def call_api(self, action, **params):
        assert action == "send_private_msg"
        return {"message_id": str(uuid4())}

    async def send(self, **params):
        return {"message_id": str(uuid4())}


async def _qq_turn(env):
    bot = _QQBot()
    env.gateway.registry.connect(bot, provider_id="snowluma")
    result = await env.harness.processor.handle(
        InboundMessage(
            message_id="qq-first",
            event_type="message:test",
            platform="qq",
            bot_user_id=bot.self_id,
            scope_type=ScopeType.PRIVATE,
            sender=SenderIdentity("42", nickname="QQ person"),
            text="QQ first",
        ),
        OneBotSender(bot, None),
    )
    return result


async def test_distinct_platform_people_have_independent_chat_buckets(telegram_env, database):
    env = telegram_env
    env.harness.processor._rate_limiter = SlidingWindowRateLimiter(per_user=1, per_group=20)
    await _qq_turn(env)
    before = len(env.model.requests)
    await env.gateway.receive(update(text="TG second"))
    await drain(env)
    async with database.sessions() as reader:
        qq = await find_identity_binding(reader, "42", platform="qq")
        telegram = await find_identity_binding(reader, "42", platform="telegram")
    assert qq.person_id != telegram.person_id
    assert len(env.model.requests) > before


async def test_same_person_across_platform_bindings_shares_chat_bucket(telegram_env, database):
    env = telegram_env
    env.harness.processor._rate_limiter = SlidingWindowRateLimiter(per_user=1, per_group=20)
    await _qq_turn(env)
    service, _, _ = await code_for(database)
    async with database.sessions() as reader:
        qq = await find_identity_binding(reader, "42", platform="qq")
    issued = await service.issue_for_operator(qq.person_id, str(uuid4()), str(uuid4()))
    env.harness.processor._client_links = ClientLinkCommands(service, env.gateway.registry)
    await env.gateway.receive(update(text=f"/ai link {issued['code']}"))
    await drain(env)
    async with database.sessions() as reader:
        telegram = await find_identity_binding(reader, "42", platform="telegram")
        assert telegram.person_id == qq.person_id
        started_at = telegram.started_at.replace(tzinfo=UTC)
    before = len(env.model.requests)
    current = update(2, text="same person on TG")
    # This message belongs to the newly linked stage, after the binding command;
    # the shared Telegram fixture otherwise carries a historical source date.
    current["message"]["date"] = ceil(started_at.timestamp())
    await env.gateway.receive(current)
    await drain(env)
    assert len(env.model.requests) == before
    sends = [params for action, params in env.wire if action == "sendMessage"]
    assert "频繁" in sends[-1]["text"]


async def test_telegram_command_and_rate_reply_keep_original_entry(telegram_env, database):
    env = telegram_env
    env.harness.processor._rate_limiter = SlidingWindowRateLimiter(per_user=1, per_group=20)
    await env.gateway.receive(update(text="/ai ping"))
    await drain(env)
    await env.gateway.receive(update(2, text="/ai ping"))
    await drain(env)
    async with database.sessions() as reader:
        rows = list(await reader.scalars(select(ChatEventModel).order_by(ChatEventModel.id)))
    inbound = [row for row in rows if row.direction == "inbound"]
    outbound = [row for row in rows if row.direction == "outbound"]
    assert len(inbound) == len(outbound) == 2
    assert all(
        row.canonical_conversation_id == inbound[0].canonical_conversation_id for row in rows
    )
    assert all(row.author_presence_id == env.gateway.presence_id for row in outbound)
    assert outbound[0].content.startswith("pong (")
    assert "频繁" in outbound[1].content
    assert not env.model.requests


async def _redeem_reply(env, reply, server, target):
    code = re.search(r"绑定码：([A-Za-z0-9_-]{32})(?![A-Za-z0-9_-])", reply).group(1)
    assert len(code) == 32 and "10 分钟" in reply and "使用一次" in reply
    root = FastAPI()
    if server:
        link = reply.split("绑定链接：", 1)[1]
        params = parse_qs(urlsplit(link).query)
        assert params == {"server": [server.rstrip("/")], "code": [code]}
        root.mount(urlsplit(server).path.rstrip("/"), env.app)
        base = params["server"][0]
    else:
        assert "yuki://pair" not in reply
        root.mount("/role", env.app)
        base = "http://test/role"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(root), base_url=base) as client:
        meta = await client.get("client/v1/meta")
        assert meta.status_code == 200 and meta.json()["data"]["character"]["id"]
        paired = await pair(client, code)
        me = await client.get("client/v1/me", headers=bearer(paired["token"]))
        assert me.json()["data"]["person_id"] == target
        reused = await client.post(
            "client/v1/pair", json={"code": code, "device_label": "duplicate"}
        )
        assert reused.status_code == 401


@pytest.mark.parametrize("server", ["", "https://example.com/Roles/Yuki/"])
async def test_qq_link_reply_pairs_through_the_actual_prefixed_api_once(app_env, database, server):
    env = app_env
    registry, bot, identities, target, _ = await stack(database)
    env.harness.processor._client_links = ClientLinkCommands(
        identities, registry, public_url=server
    )
    sender = MemorySender()
    sender.bot = bot
    result = await env.harness.processor.handle(
        _message(message_id="issue-real-code", text="/ai link"), sender
    )
    assert result.reason == "client_link" and len(sender.messages) == 1
    assert not env.calls
    await _redeem_reply(env, sender.messages[0].text, server, target)
    async with database.sessions() as session:
        assert not list(await session.scalars(select(ChatEventModel)))


async def test_telegram_native_link_reply_keeps_the_exact_code_for_actual_api_pairing(
    telegram_env, app_env, database
):
    env = telegram_env
    service, target, issued = await code_for(database)
    server = "https://example.com/roles/Telegram"
    env.harness.processor._client_links = ClientLinkCommands(
        service, env.gateway.registry, public_url=server
    )
    await env.gateway.receive(update(text=f"/ai link {issued['code']}"))
    await drain(env)
    await env.gateway.receive(update(2, text="/ai link"))
    await drain(env)
    sends = [params for method, params in env.wire if method == "sendMessage"]
    assert len(sends) == 2 and not env.model.requests
    # Telegram renders the existing HTML transport before the user copies it.
    assert sends[-1]["parse_mode"] == "HTML"
    await _redeem_reply(app_env, unescape(sends[-1]["text"]), server, target)
    async with database.sessions() as session:
        assert not list(await session.scalars(select(ChatEventModel)))
