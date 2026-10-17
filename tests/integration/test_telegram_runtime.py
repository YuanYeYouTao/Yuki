"""Native MockHTTP Telegram → real SQLite/Processor/Main/Social, without platform traffic."""

import asyncio
import json
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select
from tests.conftest import build_harness, make_settings

from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel
from qq_ai_bot.domain.messages import ChatResponse, ToolCall, ToolFunction
from qq_ai_bot.gateway.providers import builtin_provider_catalog
from qq_ai_bot.gateway.providers.telegram import TelegramClient
from qq_ai_bot.gateway.providers.telegram_ingress import TelegramGateway
from qq_ai_bot.gateway.registry import GatewayConnectionRegistry
from qq_ai_bot.identity.canonical_repository import create_presence
from qq_ai_bot.identity.canonical_uow import CanonicalIngressUnitOfWork
from qq_ai_bot.identity.ingress import CanonicalIngressResolver
from qq_ai_bot.identity.routing import PresenceRouter
from qq_ai_bot.llm.fake import FakeLLMProvider
from qq_ai_bot.persistence.models import ChatEventModel
from qq_ai_bot.services.main_agent_contract import MainAgentContract
from qq_ai_bot.social.service import SocialService
from qq_ai_bot.workspace.short_state import ShortState
from qq_ai_bot.workspace.store import WorkspaceStore


@pytest.fixture
async def telegram_env(database, tmp_path):
    wire = []

    def http(request):
        method = request.url.path.rsplit("/", 1)[-1]
        params = json.loads(request.content)
        wire.append((method, params))
        if method == "getMe":
            result = {"id": 80001, "is_bot": True, "username": "yuki_test"}
        elif method == "getChatMember":
            result = {"status": "administrator", "user": {"id": params["user_id"]}}
        elif method == "sendMessage":
            result = {"message_id": 1000 + len(wire), "chat": {"id": params["chat_id"]}}
        else:
            raise AssertionError(method)
        return httpx.Response(200, json={"ok": True, "result": result})

    def respond(request):
        if request.messages[-1].role == "tool":
            return "done"
        return ChatResponse(
            "",
            0,
            tool_calls=(ToolCall("send", ToolFunction("send_message", '{"text":"TG reply"}')),),
        )

    model = FakeLLMProvider(respond)
    harness = build_harness(database, make_settings(database.url), model)
    registry = GatewayConnectionRegistry(providers=builtin_provider_catalog())
    router = PresenceRouter(database, registry)
    harness.processor._canonical_ingress = CanonicalIngressResolver(database, registry, router)
    harness.processor._canonical_uow = CanonicalIngressUnitOfWork(database, router)
    social = SocialService(database, router, harness.scoped_events)
    harness.processor._chat._tools.social_service = social
    store = WorkspaceStore(tmp_path / "workspace")
    harness.processor._chat.runtime.runner.main_contract = MainAgentContract(
        harness.processor._chat, ShortState(store)
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(http)) as transport:
        client = TelegramClient("offline-test-token", client=transport)
        await client.get_me()
        async with database.immediate_session() as session:
            presence = await create_presence(
                session,
                external_account_id=client.self_id,
                platform="telegram",
                now=datetime.now(UTC),
            )
        registry.connect(client, provider_id="telegram", presence_id=presence.id)
        gateway = TelegramGateway(
            database, registry, router, harness.processor, harness.scoped_events, client
        )
        gateway.presence_id = presence.id
        yield SimpleNamespace(
            gateway=gateway, model=model, wire=wire, social=social, harness=harness
        )
        await gateway.close()


def update(identifier=1, *, chat=42, topic=None, text="hello", edit=False):
    message = {
        "message_id": 1 if edit else identifier,
        "date": 1792070000,
        "from": {"id": 42, "first_name": "P", "is_bot": False},
        "chat": {"id": chat, "type": "private" if chat > 0 else "supergroup", "title": "G"},
        "text": text,
    }
    if topic is not None:
        message["message_thread_id"] = topic
    if edit:
        message["edit_date"] = message["date"] + identifier
    return {"update_id": identifier, "edited_message" if edit else "message": message}


async def drain(env):
    if env.gateway._turns:
        await asyncio.wait_for(asyncio.gather(*tuple(env.gateway._turns)), 10)


async def test_native_private_main_reply_and_duplicate_keep_original_event(telegram_env, database):
    env = telegram_env
    await env.gateway.receive(update())
    await drain(env)
    assert len(env.model.requests) == 2
    sends = [params for method, params in env.wire if method == "sendMessage"]
    assert len(sends) == 1 and sends[0]["chat_id"] == "42"
    assert sends[0]["text"] == "TG reply"
    async with database.sessions() as session:
        rows = list(await session.scalars(select(ChatEventModel).order_by(ChatEventModel.id)))
    assert len(rows) == 2 and rows[0].author_person_id and rows[1].author_presence_id
    assert rows[0].canonical_conversation_id == rows[1].canonical_conversation_id
    await env.gateway.receive(update())
    await drain(env)
    assert len(env.model.requests) == 2
    assert all(
        "call_onebot_api" not in [tool.name for tool in request.tools]
        for request in env.model.requests
    )


async def test_edit_updates_original_event_without_model_or_new_delivery(telegram_env, database):
    env = telegram_env
    await env.gateway.receive(update())
    await drain(env)
    await env.gateway.receive(update(3, text="new", edit=True))
    await env.gateway.receive(update(2, text="older", edit=True))
    async with database.sessions() as session:
        rows = list(await session.scalars(select(ChatEventModel).order_by(ChatEventModel.id)))
    assert len(rows) == 2 and rows[0].content == "new"
    assert len(env.model.requests) == 2


async def test_topics_keep_original_reply_destination_and_one_space(telegram_env, database):
    env = telegram_env
    # A real explicit request reaches Main regardless of optional JEV scheduling.
    for identifier, topic in ((1, 10), (2, 20)):
        await env.gateway.receive(update(identifier, chat=-10042, topic=topic, text="!ai hello"))
        await drain(env)
    async with database.sessions() as session:
        conversations = list(
            await session.scalars(
                select(CanonicalConversationModel).where(CanonicalConversationModel.kind == "space")
            )
        )
    assert len(conversations) == 2
    assert conversations[0].space_id == conversations[1].space_id
    sends = [params for method, params in env.wire if method == "sendMessage"]
    assert [params["message_thread_id"] for params in sends] == [10, 20]
    assert all(params["chat_id"] == "-10042" for params in sends)


async def test_forget_qq_same_number_preserves_telegram_conversation_and_work(
    telegram_env, database
):
    from qq_ai_bot.domain.conversations import ConversationScope
    from qq_ai_bot.identity.canonical_repository import ensure_person, ensure_presence
    from qq_ai_bot.persistence.people_repository import PeopleRepository
    from qq_ai_bot.runtime.work_repository import WorkRepository

    env = telegram_env
    await env.gateway.receive(update(text="the number 42 is ordinary text"))
    await drain(env)
    async with database.sessions() as session:
        event = await session.scalar(
            select(ChatEventModel).where(ChatEventModel.direction == "inbound")
        )
        conversation = await session.get(
            CanonicalConversationModel, event.canonical_conversation_id
        )
        original_id, generation, person = (
            conversation.id,
            conversation.generation,
            event.author_person_id,
        )
    async with database.immediate_session() as session:
        qq_person = await ensure_person(session, "42")
        await ensure_presence(session, "80001")
    await env.harness.scoped_events.append(
        scope=ConversationScope.private("80001", "42"),
        platform_message_id="qq-original",
        sender_user_id="42",
        direction="inbound",
        content="QQ owner",
    )
    repository = WorkRepository(database)
    lease = await repository.acquire(original_id, generation)
    try:
        work = await repository.accept(
            lease,
            source_key=f"event:{event.id}",
            source={
                "origin": "user_message",
                "principal_kind": "person",
                "actor_person_id": person,
                "trigger_event_id": event.id,
                "conversation_id": original_id,
                "presence_id": env.gateway.presence_id,
                "generation": generation,
            },
            goal="original TG work",
        )
    finally:
        await repository.release(lease)
    assert qq_person != person
    assert await PeopleRepository(database).delete_person_id(qq_person)
    async with database.sessions() as session:
        kept = await session.get(ChatEventModel, event.id)
        after = await session.get(CanonicalConversationModel, original_id)
    assert kept is not None and kept.author_person_id == person and "42" in kept.content
    assert after is not None and after.generation == generation
    retained = await repository.get(work["id"])
    assert retained is not None and retained["conversation_id"] == original_id


async def test_another_platform_uses_the_same_processor_and_social_contract(telegram_env, database):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from qq_ai_bot.domain.conversations import ScopeType
    from qq_ai_bot.domain.messages import InboundMessage, SenderIdentity
    from qq_ai_bot.gateway.provider import GatewayConnectionProfile, GatewayProviderCatalog
    from qq_ai_bot.gateway.providers.app.social import AppSocialOperations
    from qq_ai_bot.gateway.social import TransportReceipt

    sent = []

    class NativeOperations(AppSocialOperations):
        async def send(self, handle, target, message):
            sent.append((handle, target, message))
            return TransportReceipt("native-receipt")

    class ExtraProvider:
        provider_id = "extra"
        social = NativeOperations()

        def media(self, handle):
            return None

        def project_inbound(self, message, yuki_ids):
            return message

        def describe_connection(self, handle):
            return GatewayConnectionProfile(
                "extra", "extra", handle.self_id, frozenset({"send_private"})
            )

    env = telegram_env
    registry = GatewayConnectionRegistry(providers=GatewayProviderCatalog((ExtraProvider(),)))
    router = PresenceRouter(database, registry)
    async with database.immediate_session() as session:
        presence = await create_presence(
            session, external_account_id="80001", platform="extra", now=datetime.now(UTC)
        )
    handle = SimpleNamespace(self_id="80001")
    registry.connect(handle, provider_id="extra", presence_id=presence.id)
    env.harness.processor._canonical_ingress = CanonicalIngressResolver(database, registry, router)
    env.harness.processor._canonical_uow = CanonicalIngressUnitOfWork(database, router)
    env.harness.processor._chat._tools.social_service = SocialService(
        database, router, env.harness.scoped_events
    )
    result = await env.harness.processor.handle(
        InboundMessage(
            "original",
            "message:extra",
            ScopeType.PRIVATE,
            SenderIdentity("42"),
            "hello",
            bot_user_id="80001",
            platform="extra",
        ),
        SimpleNamespace(
            bot=handle, send=AsyncMock(side_effect=AssertionError("Main must use Social"))
        ),
    )
    assert result.reason == "chat" and len(sent) == 1
    assert sent[0][0] is handle and sent[0][1].external_id == "42"
    assert sent[0][2].text == "TG reply"
    async with database.sessions() as session:
        rows = list(await session.scalars(select(ChatEventModel)))
    assert len(rows) == 2 and rows[0].canonical_conversation_id == rows[1].canonical_conversation_id
