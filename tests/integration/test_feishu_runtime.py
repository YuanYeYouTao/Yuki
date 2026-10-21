"""Native Feishu HTTP → real SQLite/Processor/Main/Social without live traffic."""

import asyncio
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import select
from tests.conftest import build_harness, make_settings
from websockets.asyncio.server import serve

from yuki.conversation.canonical_db_models import CanonicalConversationModel
from yuki.domain.messages import ChatResponse, ToolCall, ToolFunction
from yuki.gateway.providers import builtin_provider_catalog
from yuki.gateway.providers.feishu import FeishuClient, encode_account_id
from yuki.gateway.providers.feishu_ingress import FeishuGateway
from yuki.gateway.registry import GatewayConnectionRegistry
from yuki.identity.canonical_repository import create_presence
from yuki.identity.canonical_uow import CanonicalIngressUnitOfWork
from yuki.identity.db_models import CanonicalPersonModel, IdentityBindingModel
from yuki.identity.ingress import CanonicalIngressResolver
from yuki.identity.routing import PresenceRouter
from yuki.llm.fake import FakeLLMProvider
from yuki.persistence.models import ChatEventModel
from yuki.services.group_recovery import NativeGroupRecovery
from yuki.services.main_agent_contract import MainAgentContract
from yuki.social.service import SocialService
from yuki.workspace.short_state import ShortState
from yuki.workspace.store import WorkspaceStore


@pytest.fixture
async def feishu_env(database, tmp_path):
    wire = []

    def http(request):
        path = request.url.path
        params = json.loads(request.content) if request.content else {}
        wire.append((request.method, path, dict(request.url.params), params))
        if path == "/open-apis/auth/v3/tenant_access_token/internal":
            payload = {"tenant_access_token": "offline-token", "expire": 7200}
        elif path == "/open-apis/bot/v3/info":
            payload = {"bot": {"open_id": "ou_yuki", "app_name": "Yuki"}}
        elif path.endswith("/members/is_in_chat"):
            payload = {"data": {"is_in_chat": True}}
        elif path.endswith("/reply") or (path.endswith("/messages") and request.method == "POST"):
            payload = {"data": {"message_id": f"om_sent_{len(wire)}"}}
        elif path == "/open-apis/im/v1/messages" and request.method == "GET":
            payload = {"data": {"items": [{"message_id": "om_thread_root"}], "has_more": False}}
        else:
            raise AssertionError((request.method, path))
        return httpx.Response(200, json={"code": 0, **payload})

    def respond(request):
        if request.messages[-1].role == "tool":
            return "done"
        return ChatResponse(
            "",
            0,
            tool_calls=(ToolCall("send", ToolFunction("send_message", '{"text":"Feishu reply"}')),),
        )

    model = FakeLLMProvider(respond)
    harness = build_harness(database, make_settings(database.url), model)
    registry = GatewayConnectionRegistry(providers=builtin_provider_catalog())
    router = PresenceRouter(database, registry)
    harness.processor._canonical_ingress = CanonicalIngressResolver(database, registry, router)
    harness.processor._canonical_uow = CanonicalIngressUnitOfWork(database, router)
    harness.processor._group_recovery = NativeGroupRecovery(
        harness.settings, database, registry, router
    )
    social = SocialService(database, router, harness.scoped_events)
    harness.processor._chat._tools.social_service = social
    harness.processor._chat.runtime.runner.main_contract = MainAgentContract(
        harness.processor._chat, ShortState(WorkspaceStore(tmp_path / "workspace"))
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(http)) as transport:
        client = FeishuClient("cli_test", "offline-secret", client=transport)
        await client.get_me()
        async with database.immediate_session() as session:
            presence = await create_presence(
                session,
                external_account_id=client.self_id,
                platform=client.platform,
                now=datetime.now(UTC),
            )
        registry.connect(client, provider_id=client.platform, presence_id=presence.id)
        gateway = FeishuGateway(
            database, registry, router, harness.processor, harness.scoped_events, client
        )
        gateway.presence_id = presence.id
        yield SimpleNamespace(
            gateway=gateway,
            model=model,
            wire=wire,
            social=social,
            harness=harness,
            registry=registry,
            client=client,
        )
        await gateway.close()


def update(identifier="om_1", *, group=False, thread=None, text="hello", message_type="text"):
    message = {
        "message_id": identifier,
        "create_time": "1792070000000",
        "chat_id": "oc_group" if group else "oc_private",
        "chat_type": "group" if group else "p2p",
        "message_type": message_type,
        "content": json.dumps({"text": text}),
    }
    if thread is not None:
        message["thread_id"] = thread
        message["root_id"] = "om_root_" + thread.removeprefix("omt_")
    return {
        "schema": "2.0",
        "header": {
            "event_type": "im.message.receive_v1",
            "event_id": identifier,
            "app_id": "cli_test",
        },
        "event": {
            "sender": {"sender_id": {"open_id": "ou_person"}, "sender_type": "user"},
            "message": message,
        },
    }


async def drain(env):
    if env.gateway._turns:
        await asyncio.wait_for(asyncio.gather(*tuple(env.gateway._turns)), 10)


async def test_native_private_uses_common_main_social_and_durable_duplicate(feishu_env, database):
    env = feishu_env
    await env.gateway.receive(update())
    await drain(env)
    sends = [
        (query, body)
        for method, path, query, body in env.wire
        if method == "POST" and path == "/open-apis/im/v1/messages"
    ]
    assert len(sends) == 1
    query, body = sends[0]
    assert query == {"receive_id_type": "open_id"}
    assert body["receive_id"] == "ou_person"
    assert json.loads(body["content"]) == {"text": "Feishu reply"}
    async with database.sessions() as session:
        rows = list(await session.scalars(select(ChatEventModel).order_by(ChatEventModel.id)))
    assert len(rows) == 2 and rows[0].author_person_id and rows[1].author_presence_id
    assert rows[0].sender_user_id == "cli_test:ou_person"
    assert rows[0].canonical_conversation_id == rows[1].canonical_conversation_id
    count = len(env.model.requests)
    await env.gateway.receive(update())
    replay = update()
    replay["event"]["message"]["content"] = json.dumps({"text": "changed retry body"})
    await env.gateway.receive(replay)
    await drain(env)
    assert len(env.model.requests) == count
    assert "call_onebot_api" not in {tool.name for tool in env.model.requests[0].tools}


async def test_admission_returns_before_model_finishes(feishu_env, database, monkeypatch):
    env = feishu_env
    entered, release = asyncio.Event(), asyncio.Event()
    complete = env.model._complete
    attempts = []

    async def blocked(request):
        attempts.append(request)
        entered.set()
        await release.wait()
        return await complete(request)

    monkeypatch.setattr(env.model, "_complete", blocked)
    await asyncio.wait_for(env.gateway.receive(update()), 2)
    await asyncio.wait_for(entered.wait(), 2)
    assert env.gateway._turns
    async with database.sessions() as session:
        event = await session.scalar(select(ChatEventModel))
        assert event is not None and event.platform_message_id == "om_1"
    # Losing an ACK while this Main is still running doesn't buy another request.
    await asyncio.wait_for(env.gateway.receive(update()), 2)
    assert len(attempts) == 1 and len(env.gateway._turns) == 1
    release.set()
    await drain(env)


async def test_group_cold_admission_and_native_mention_keep_platform_identity(feishu_env, database):
    env = feishu_env
    incoming = update(group=True, text="before @_user_1 after")
    incoming["event"]["message"]["mentions"] = [
        {"key": "@_user_1", "id": {"open_id": "ou_yuki"}, "name": "Yuki"}
    ]
    await env.gateway.receive(incoming)
    await drain(env)
    async with database.sessions() as session:
        events = list(await session.scalars(select(ChatEventModel).order_by(ChatEventModel.id)))
        conversation = await session.get(
            CanonicalConversationModel, events[0].canonical_conversation_id
        )
    assert conversation.kind == "space" and conversation.space_id
    assert "@Yuki" in events[0].content and "@_user_1" not in events[0].content
    assert events[0].sender_user_id == "cli_test:ou_person"
    assert any(path.endswith("/members/is_in_chat") for _m, path, _q, _b in env.wire)
    assert env.model.requests


async def test_topics_keep_real_threads_and_share_space(feishu_env, database):
    env = feishu_env
    for number, thread in enumerate(("omt_one", "omt_two")):
        await env.gateway.receive(
            update(f"om_{number}", group=True, thread=thread, text="!ai hello")
        )
        await drain(env)
    async with database.sessions() as session:
        conversations = list(await session.scalars(select(CanonicalConversationModel)))
    assert len(conversations) == 2
    assert {row.thread_id for row in conversations} == {"om_root_one", "om_root_two"}
    assert len({row.space_id for row in conversations}) == 1
    replies = [
        body for method, path, _q, body in env.wire if method == "POST" and path.endswith("/reply")
    ]
    assert len(replies) == 2 and all(body["reply_in_thread"] for body in replies)
    assert {path for method, path, _q, _b in env.wire if path.endswith("/reply")} == {
        "/open-apis/im/v1/messages/om_root_one/reply",
        "/open-apis/im/v1/messages/om_root_two/reply",
    }


@pytest.mark.parametrize("native_at", [False, True])
async def test_native_group_control_uses_current_person_without_main(
    feishu_env, database, native_at
):
    env = feishu_env
    await env.gateway.receive(update())
    await drain(env)
    async with database.immediate_session() as session:
        binding = await session.scalar(
            select(IdentityBindingModel).where(
                IdentityBindingModel.platform == "feishu",
                IdentityBindingModel.external_account_id
                == encode_account_id("cli_test", "ou_person"),
            )
        )
        person = await session.get(CanonicalPersonModel, binding.person_id)
        person.is_superuser = True
    await env.gateway.receive(update("om_group", group=True, text="!ai hello"))
    await drain(env)
    before = len(env.model.requests)

    def control(action):
        incoming = update(f"om_{action}", group=True, text=f"/ai admin group current {action}")
        if native_at:
            incoming["event"]["message"]["content"] = json.dumps(
                {"text": f"@_user_1 /ai admin group current {action}"}
            )
            incoming["event"]["message"]["mentions"] = [
                {"key": "@_user_1", "id": {"open_id": "ou_yuki"}, "name": "Yuki"},
            ]
        return incoming

    await env.gateway.receive(control("disable"))
    await drain(env)
    await env.gateway.receive(control("enable"))
    await drain(env)
    assert len(env.model.requests) == before
    texts = [
        json.loads(body["content"])["text"]
        for method, path, _q, body in env.wire
        if method == "POST" and path == "/open-apis/im/v1/messages"
    ]
    assert texts[-1].startswith("已启用当前群")


async def test_real_websocket_reconnect_updates_registry_for_original_presence(
    feishu_env, monkeypatch
):
    env = feishu_env
    env.registry.disconnect(env.client)
    connections = 0
    reconnected = asyncio.Event()

    async def server(connection):
        nonlocal connections
        connections += 1
        if connections == 1:
            await connection.close()
        else:
            await connection.recv()  # First protocol ping follows Registry.connect.
            reconnected.set()
            await connection.wait_closed()

    async with serve(server, "127.0.0.1", 0) as listener:
        port = listener.sockets[0].getsockname()[1]
        monkeypatch.setattr(
            env.client,
            "websocket_endpoint",
            AsyncMock(
                return_value={
                    "code": 0,
                    "data": {
                        "URL": f"ws://127.0.0.1:{port}/?service_id=9&device_id=local",
                        "ClientConfig": {
                            "ReconnectNonce": 0,
                            "ReconnectInterval": 0,
                            "ReconnectCount": 2,
                        },
                    },
                }
            ),
        )
        original_presence = env.gateway.presence_id
        await env.gateway.start()
        await asyncio.wait_for(reconnected.wait(), 2)
        active = env.registry.resolve_active(original_presence)
        assert active.bot is env.client and active.snapshot.generation == 3
        assert env.gateway.presence_id == original_presence
        assert (await env.gateway.health())["healthy"]
        assert not env.model.requests
        await env.gateway.close()
        assert not env.registry.has_any_active(platform="feishu")
