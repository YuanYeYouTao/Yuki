"""Native MockHTTP Telegram → real SQLite/Processor/Main/Social, without platform traffic."""

import asyncio
import json
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select
from tests.conftest import build_harness, make_settings
from tests.support.event_seed import seed_scoped_event
from tests.support.service_dependencies import social_service

from yuki.conversation.canonical_db_models import CanonicalConversationModel
from yuki.domain.messages import ChatResponse, ToolCall, ToolFunction
from yuki.gateway.providers import builtin_provider_catalog
from yuki.gateway.providers.telegram import TelegramClient
from yuki.gateway.providers.telegram_ingress import TelegramGateway
from yuki.gateway.registry import GatewayConnectionRegistry
from yuki.identity.canonical_repository import create_presence
from yuki.identity.canonical_uow import CanonicalIngressUnitOfWork
from yuki.identity.db_models import CanonicalPersonModel, CanonicalSpaceModel, IdentityBindingModel
from yuki.identity.ingress import CanonicalIngressResolver
from yuki.identity.routing import PresenceRouter
from yuki.llm.fake import FakeLLMProvider
from yuki.persistence.models import ChatEventModel
from yuki.services.group_recovery import NativeGroupRecovery
from yuki.services.main_agent_contract import MainAgentContract
from yuki.workspace.short_state import ShortState
from yuki.workspace.store import WorkspaceStore


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
        elif method == "sendChatAction":
            result = True
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
            tool_calls=(ToolCall("send", ToolFunction("send", '{"text":"TG reply"}')),),
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
    social = social_service(database, router, harness.scoped_events)
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
    async with database.read_session() as session:
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


@pytest.mark.parametrize("paused", [False, True])
async def test_native_group_enable_recovers_disabled_group_without_chat_or_model(
    telegram_env, database, paused
):
    from yuki.conversation.canonical_db_models import (
        ControlCommandReceiptModel,
        SpaceActiveRouteModel,
        SpaceBindingIngestRouteModel,
    )
    from yuki.identity.db_models import SpaceBindingModel

    env = telegram_env
    await env.gateway.receive(update(text="establish sender"))
    await drain(env)
    async with database.immediate_session() as session:
        binding = await session.scalar(
            select(IdentityBindingModel).where(
                IdentityBindingModel.platform == "telegram",
                IdentityBindingModel.external_account_id == "42",
            )
        )
        person = await session.get(CanonicalPersonModel, binding.person_id)
        person.is_superuser = True
    await env.gateway.receive(update(2, chat=-10042, text="!ai establish group"))
    await drain(env)
    await env.gateway.receive(update(3, chat=-10042, text="/ai admin group current disable"))
    await drain(env)
    async with database.immediate_session() as session:
        space = await session.scalar(
            select(CanonicalSpaceModel).where(CanonicalSpaceModel.name == "G")
        )
        assert not space.enabled
        space_id = space.id
        binding = await session.scalar(
            select(SpaceBindingModel).where(SpaceBindingModel.space_id == space_id)
        )
        ingest = await session.get(SpaceBindingIngestRouteModel, binding.id)
        route = await session.get(SpaceActiveRouteModel, space_id)
        if paused:
            ingest.paused = True
            if route is not None:
                route.paused = True
        before_events = list(await session.scalars(select(ChatEventModel.id)))
        conversation = await session.scalar(
            select(CanonicalConversationModel).where(
                CanonicalConversationModel.space_id == space_id
            )
        )
        before_generation = conversation.generation
    before_requests = len(env.model.requests)
    await env.gateway.receive(update(4, chat=-10042, text="/ai admin group CURRENT enable"))
    await drain(env)
    async with database.read_session() as session:
        space = await session.get(CanonicalSpaceModel, space_id)
        ingest = await session.get(SpaceBindingIngestRouteModel, binding.id)
        route = await session.get(SpaceActiveRouteModel, space_id)
        assert space.enabled and not ingest.paused and not route.paused
        assert list(await session.scalars(select(ChatEventModel.id))) == before_events
        conversation = await session.get(CanonicalConversationModel, conversation.id)
        assert conversation.generation == before_generation
        receipt = await session.scalar(
            select(ControlCommandReceiptModel).where(
                ControlCommandReceiptModel.result_resource_id == space_id,
                ControlCommandReceiptModel.operation_kind.is_(None),
            )
        )
        assert receipt is not None and receipt.status == "succeeded"
    assert len(env.model.requests) == before_requests
    assert [p["text"] for method, p in env.wire if method == "sendMessage"][-1].startswith(
        "已启用当前群"
    )


async def test_group_recovery_uses_platform_person_and_current_superuser(telegram_env, database):
    from tests.support.canonical_identity import ensure_person

    env = telegram_env
    await env.gateway.receive(update(text="establish sender"))
    await drain(env)
    await env.gateway.receive(update(2, chat=-10042, text="!ai establish group"))
    await drain(env)
    async with database.immediate_session() as session:
        qq_id = await ensure_person(session, "42")
        qq_person = await session.get(CanonicalPersonModel, qq_id)
        qq_person.is_superuser = True
        space = await session.scalar(
            select(CanonicalSpaceModel).where(CanonicalSpaceModel.name == "G")
        )
        space_id = space.id
        space.enabled = False
    before_requests = len(env.model.requests)
    await env.gateway.receive(update(3, chat=-10042, text="/ai admin group current enable"))
    await drain(env)
    async with database.read_session() as session:
        space = await session.get(CanonicalSpaceModel, space_id)
        assert not space.enabled
    assert len(env.model.requests) == before_requests
    assert [p["text"] for method, p in env.wire if method == "sendMessage"][-1].startswith(
        "无法恢复"
    )


async def test_group_recovery_keeps_original_merged_actor(telegram_env, database):
    from tests.support.person_merge import merge_persons

    from yuki.conversation.canonical_db_models import ControlCommandReceiptModel
    from yuki.identity.canonical_repository import create_person_binding

    env = telegram_env
    await env.gateway.receive(update(text="establish sender"))
    await drain(env)
    async with database.immediate_session() as session:
        binding = await session.scalar(
            select(IdentityBindingModel).where(
                IdentityBindingModel.platform == "telegram",
                IdentityBindingModel.external_account_id == "42",
            )
        )
        original_person = binding.person_id
        target = await create_person_binding(
            session,
            external_account_id="merge-target",
            display_name="Merge target",
            now=datetime.now(UTC),
            platform="telegram",
        )
        person = await session.get(CanonicalPersonModel, target.person_id)
        person.is_superuser = True
        await merge_persons(session, original_person, person.id)
    before_requests = len(env.model.requests)
    await env.gateway.receive(update(2, chat=-10042, text="/ai admin group current enable"))
    await drain(env)
    async with database.read_session() as session:
        receipt = await session.scalar(
            select(ControlCommandReceiptModel).where(
                ControlCommandReceiptModel.principal_id == original_person
            )
        )
        assert receipt is not None and receipt.status == "succeeded"
    assert len(env.model.requests) == before_requests
    assert [p["text"] for method, p in env.wire if method == "sendMessage"][-1].startswith(
        "已启用当前群"
    )


async def test_edit_updates_original_event_without_model_or_new_delivery(telegram_env, database):
    env = telegram_env
    await env.gateway.receive(update())
    await drain(env)
    await env.gateway.receive(update(3, text="new", edit=True))
    await env.gateway.receive(update(2, text="older", edit=True))
    async with database.read_session() as session:
        rows = list(await session.scalars(select(ChatEventModel).order_by(ChatEventModel.id)))
    assert len(rows) == 2 and rows[0].content == "new"
    assert len(env.model.requests) == 2


async def test_topics_keep_original_reply_destination_and_one_space(telegram_env, database):
    env = telegram_env
    # A real explicit request reaches Main regardless of optional JEV scheduling.
    for identifier, topic in ((1, 10), (2, 20)):
        await env.gateway.receive(update(identifier, chat=-10042, topic=topic, text="!ai hello"))
        await drain(env)
    async with database.read_session() as session:
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
    from tests.support.canonical_identity import ensure_person, ensure_presence

    from yuki.domain.conversations import ConversationScope
    from yuki.persistence.people_repository import PeopleRepository
    from yuki.runtime.work_repository import WorkRepository

    env = telegram_env
    await env.gateway.receive(update(text="the number 42 is ordinary text"))
    await drain(env)
    async with database.read_session() as session:
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
    await seed_scoped_event(
        env.harness.scoped_events,
        scope=ConversationScope.private("80001", "42"),
        origin_key="qq-original",
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
    async with database.read_session() as session:
        kept = await session.get(ChatEventModel, event.id)
        after = await session.get(CanonicalConversationModel, original_id)
    assert kept is not None and kept.author_person_id == person and "42" in kept.content
    assert after is not None and after.generation == generation
    retained = await repository.get(work["id"])
    assert retained is not None and retained["conversation_id"] == original_id


async def test_another_platform_uses_the_same_processor_and_social_contract(telegram_env, database):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from yuki.domain.conversations import ScopeType
    from yuki.domain.messages import InboundMessage, SenderIdentity
    from yuki.gateway.provider import GatewayConnectionProfile, GatewayProviderCatalog
    from yuki.gateway.providers.app.social import AppSocialOperations
    from yuki.gateway.social import TransportReceipt

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

        def activity(self, handle, target):
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
    env.harness.processor._chat._tools.social_service = social_service(
        database, router, env.harness.scoped_events
    )
    result = await env.harness.processor.handle(
        InboundMessage(
            "original",
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
    async with database.read_session() as session:
        rows = list(await session.scalars(select(ChatEventModel)))
    assert len(rows) == 2 and rows[0].canonical_conversation_id == rows[1].canonical_conversation_id
