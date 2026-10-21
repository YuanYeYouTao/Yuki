"""Real personal HTTP → App Gateway → Processor → fake Main → local Social."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select
from tests.conftest import build_harness, make_settings
from tests.unit.test_admin_api import admin as admin_fixture
from tests.unit.test_client_api import bearer, pair, person_and_code

from yuki.admin_api.http import attach_admin_api
from yuki.client_api.files import ClientFiles
from yuki.client_api.http import ClientApi, attach_client_api
from yuki.client_api.messages import ClientMessages
from yuki.client_api.prompts import ClientPrompts
from yuki.client_api.views import ClientViews
from yuki.client_api.works import ClientWorks
from yuki.conversation.media_service import ConversationMediaService
from yuki.conversation.uploads import ConversationUploads
from yuki.domain.messages import ChatResponse, ToolCall, ToolFunction
from yuki.gateway.providers import builtin_provider_catalog
from yuki.gateway.providers.app.ingress import AppGateway
from yuki.gateway.registry import GatewayConnectionRegistry
from yuki.identity.canonical_repository import find_presence
from yuki.identity.canonical_uow import CanonicalIngressUnitOfWork
from yuki.identity.ingress import CanonicalIngressResolver
from yuki.identity.routing import PresenceRouter
from yuki.llm.fake import FakeLLMProvider
from yuki.persistence.models import ChatEventModel
from yuki.services.image_preprocessor import ImagePreprocessor
from yuki.services.main_agent_contract import MainAgentContract
from yuki.services.media_resolver import MediaResolver
from yuki.social.service import SocialService
from yuki.workspace.short_state import ShortState
from yuki.workspace.store import WorkspaceStore

admin = admin_fixture


@pytest.fixture
async def app_env(database, admin, tmp_path):
    _, api, *_ = admin
    calls = []

    def respond(request):
        calls.append(request)
        if request.messages[-1].role == "tool":
            return "已发送"
        return ChatResponse(
            content="",
            latency_seconds=0,
            tool_calls=(
                ToolCall(
                    id=str(uuid4()),
                    function=ToolFunction(name="send_message", arguments='{"text":"App reply"}'),
                ),
            ),
        )

    provider = FakeLLMProvider(respond)
    harness = build_harness(database, make_settings(database.url), provider)
    registry = GatewayConnectionRegistry(
        providers=builtin_provider_catalog(
            qq_transfer_root=tmp_path / "transfer", qq_gateway_root="/transfer"
        )
    )
    router = PresenceRouter(database, registry)
    async with database.sessions() as session:
        assert await find_presence(session, "yuki-app", platform="app") is not None
    harness.processor._canonical_ingress = CanonicalIngressResolver(database, registry, router)
    harness.processor._canonical_uow = CanonicalIngressUnitOfWork(database, router)
    social = SocialService(database, router, harness.scoped_events)
    harness.processor._chat._tools.social_service = social
    store = WorkspaceStore(tmp_path / "workspace")
    harness.processor._chat.runtime.runner.main_contract = MainAgentContract(
        harness.processor._chat, ShortState(store)
    )
    resolver = MediaResolver()
    preprocessor = ImagePreprocessor(
        max_dimension=1024, max_pixels=16_000_000, max_prepared_bytes=6_291_456, gif_max_frames=8
    )
    media = ConversationMediaService(database, tmp_path / "media", resolver, preprocessor)
    harness.processor._conversation_media = media
    social.workspace = store
    gateway = AppGateway(database, registry, harness.processor, social)
    await gateway.start()
    prompts = ClientPrompts(
        api.identities,
        {"app": gateway},
        harness.processor._chat.runtime.bindings,
        concurrency=harness.concurrency,
    )
    messages = ClientMessages(api.identities, gateway, prompts=prompts)
    works = ClientWorks(api.identities, gateway)
    views = ClientViews(api.identities, works)
    files = ClientFiles(ConversationUploads(api.identities, media), views, store)
    messages.files = files
    app = FastAPI()
    attach_admin_api(app, lambda: api)
    attach_client_api(
        app,
        lambda: ClientApi(api.identities, messages, works, views, files, prompts),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield SimpleNamespace(
            client=client,
            app=app,
            gateway=gateway,
            harness=harness,
            calls=calls,
            provider=provider,
            identity=api.identities,
            service=messages,
            social=social,
            files=files,
            media=media,
            store=store,
        )
    await gateway.close()
    await media.close()
    await resolver.close()


async def drain(env):
    if env.gateway.tasks:
        await asyncio.wait_for(asyncio.gather(*tuple(env.gateway.tasks)), 10)


async def test_app_message_and_local_reply_stay_in_app_conversation(app_env, database):
    env = app_env
    person, code = await person_and_code(env.client)
    device = await pair(env.client, code)
    request = {"request_id": str(uuid4()), "text": "hello", "attachments": []}
    response = await env.client.post(
        "/client/v1/messages", json=request, headers=bearer(device["token"])
    )
    assert response.status_code == 200, response.text
    result = response.json()["data"]
    assert not result["replay"]
    await drain(env)
    async with database.sessions() as session:
        rows = list(
            await session.scalars(
                select(ChatEventModel)
                .where(ChatEventModel.canonical_conversation_id == result["conversation_id"])
                .order_by(ChatEventModel.id)
            )
        )
        assert [(row.direction, row.content) for row in rows] == [
            ("inbound", "hello"),
            ("outbound", "App reply"),
        ]
        assert rows[0].author_person_id == person and rows[0].ingress_provider == "app"
        assert rows[1].platform_message_id.startswith("social-operation:")
        assert rows[1].author_presence_id == rows[0].ingress_presence_id
    queried = await env.client.get(
        f"/client/v1/requests/{request['request_id']}", headers=bearer(device["token"])
    )
    assert queried.json()["data"]["event_id"] == result["event_id"]
    assert len(env.calls) == 2


async def test_concurrent_devices_reuse_one_message_and_one_notification(
    app_env, database, monkeypatch
):
    env = app_env
    _, code = await person_and_code(env.client)
    first = await pair(env.client, code)
    link = await env.client.post("/client/v1/link-codes", headers=bearer(first["token"]))
    second = await pair(env.client, link.json()["data"]["code"])
    coordinator = env.harness.processor._turn_coordinator
    notify = AsyncMock(wraps=coordinator.notify_message)
    monkeypatch.setattr(coordinator, "notify_message", notify)
    body = {"request_id": str(uuid4()), "text": "same request"}
    responses = await asyncio.gather(
        *(
            env.client.post("/client/v1/messages", json=body, headers=bearer(device["token"]))
            for device in (first, second)
        )
    )
    assert all(response.status_code == 200 for response in responses), [
        response.text for response in responses
    ]
    assert len({response.json()["data"]["event_id"] for response in responses}) == 1
    assert sorted(response.json()["data"]["replay"] for response in responses) == [False, True]
    await drain(env)
    assert notify.await_count == 1 and len(env.calls) == 2
    changed = await env.client.post(
        "/client/v1/messages", json={**body, "text": "changed"}, headers=bearer(second["token"])
    )
    assert changed.status_code == 409
    env.gateway.receive = AsyncMock(side_effect=AssertionError("replay constructed a new inbound"))
    replay = await env.client.post(
        "/client/v1/messages", json=body, headers=bearer(second["token"])
    )
    assert replay.status_code == 200 and replay.json()["data"]["replay"]
    env.gateway.receive.assert_not_called()
    async with database.sessions() as session:
        assert (
            await session.scalar(
                select(func.count(ChatEventModel.id)).where(ChatEventModel.direction == "inbound")
            )
            == 1
        )


async def test_person_request_namespace_and_original_authorization(app_env, database):
    env = app_env
    p, code = await person_and_code(env.client)
    a = await pair(env.client, code)
    q, code = await person_and_code(env.client)
    b = await pair(env.client, code)
    body = {"request_id": str(uuid4()), "text": "same request ID, different people"}
    first = await env.client.post("/client/v1/messages", json=body, headers=bearer(a["token"]))
    missing = await env.client.get(
        f"/client/v1/requests/{body['request_id']}", headers=bearer(b["token"])
    )
    assert missing.json()["data"] == {"found": False}
    second = await env.client.post("/client/v1/messages", json=body, headers=bearer(b["token"]))
    assert first.status_code == second.status_code == 200
    assert first.json()["data"]["conversation_id"] != second.json()["data"]["conversation_id"]
    await drain(env)
    async with database.immediate_session() as session:
        event = await session.get(ChatEventModel, first.json()["data"]["event_id"])
        event.author_person_id = q
    denied = await env.client.post("/client/v1/messages", json=body, headers=bearer(a["token"]))
    assert denied.status_code == 404
    assert "event_id" not in denied.json()
    assert p != q and len(env.calls) == 4


@pytest.mark.parametrize("different", [False, True])
async def test_competing_requests_in_different_minutes_recheck_original_body(
    app_env, monkeypatch, different
):
    from datetime import UTC, datetime, timedelta

    import yuki.domain.messages as message_models

    env = app_env
    _, code = await person_and_code(env.client)
    device = await pair(env.client, code)
    ready = asyncio.Event()
    previous = env.service._previous
    arrivals = 0

    async def concurrent(*args, **kwargs):
        nonlocal arrivals
        result = await previous(*args, **kwargs)
        if result is None:
            arrivals += 1
            if arrivals == 2:
                ready.set()
            await ready.wait()
        return result

    monkeypatch.setattr(env.service, "_previous", concurrent)
    clock = iter((datetime.now(UTC), datetime.now(UTC) + timedelta(minutes=2)))
    monkeypatch.setattr(message_models, "datetime", SimpleNamespace(now=lambda _tz: next(clock)))
    identity = str(uuid4())
    responses = await asyncio.gather(
        *(
            env.client.post(
                "/client/v1/messages",
                json={"request_id": identity, "text": text},
                headers=bearer(device["token"]),
            )
            for text in ("first", "different" if different else "first")
        )
    )
    assert sorted(response.status_code for response in responses) == (
        [200, 409] if different else [200, 200]
    ), [response.text for response in responses]
    await drain(env)
    assert len(env.calls) == 2


async def test_http_connection_lifetime_does_not_own_main_execution(app_env, monkeypatch):
    env = app_env
    _, code = await person_and_code(env.client)
    device = await pair(env.client, code)
    entered, release = asyncio.Event(), asyncio.Event()
    from yuki.llm.fake import FakeLLMProvider

    complete = FakeLLMProvider.complete

    async def waiting(provider, request):
        entered.set()
        await release.wait()
        return await complete(provider, request)

    monkeypatch.setattr(FakeLLMProvider, "complete", waiting)
    result = await asyncio.wait_for(
        env.client.post(
            "/client/v1/messages",
            json={"request_id": str(uuid4()), "text": "continue after HTTP response"},
            headers=bearer(device["token"]),
        ),
        2,
    )
    assert result.status_code == 200
    await asyncio.wait_for(entered.wait(), 2)
    assert env.gateway.tasks
    release.set()
    await drain(env)
    assert len(env.calls) == 2


async def test_local_commit_ack_loss_reads_original_social_receipt(app_env, database, monkeypatch):
    import sqlite3

    from sqlalchemy.exc import OperationalError
    from sqlalchemy.ext.asyncio import AsyncSession

    from yuki.adapters.onebot.social import OneBotSocialOperations
    from yuki.social.db_models import SocialOperationModel
    from yuki.social.models import OperationStatus

    env = app_env
    _, code = await person_and_code(env.client)
    device = await pair(env.client, code)
    finish = env.social.receipts.finish

    async def marked(*args, **kwargs):
        result = await finish(*args, **kwargs)
        if kwargs.get("status") is OperationStatus.SUCCEEDED:
            kwargs["session"].info["lose_local_ack"] = True
        return result

    commit = AsyncSession.commit

    async def lost_ack(session):
        await commit(session)
        if session.info.pop("lose_local_ack", False):
            raise OperationalError(
                "COMMIT", {}, sqlite3.OperationalError("lost local acknowledgement")
            )

    monkeypatch.setattr(env.social.receipts, "finish", marked)
    monkeypatch.setattr(AsyncSession, "commit", lost_ack)
    network = AsyncMock(side_effect=AssertionError("App attempted OneBot"))
    monkeypatch.setattr(OneBotSocialOperations, "send", network)
    result = await env.client.post(
        "/client/v1/messages",
        json={"request_id": str(uuid4()), "text": "commit once"},
        headers=bearer(device["token"]),
    )
    assert result.status_code == 200
    await drain(env)
    async with database.sessions() as session:
        receipts = list(await session.scalars(select(SocialOperationModel)))
        assert len(receipts) == 1 and receipts[0].status == "succeeded"
        assert receipts[0].platform_reference is None and receipts[0].event_id is not None
        assert (
            await session.scalar(
                select(func.count(ChatEventModel.id)).where(ChatEventModel.direction == "outbound")
            )
            == 1
        )
    network.assert_not_called()


async def test_app_source_recovers_original_platform_and_memory_owner(app_env, database):
    from yuki.persistence.event_repository import EventLedgerRepository
    from yuki.persistence.models import MemoryJobModel
    from yuki.services.execution_sources import recover_source

    env = app_env
    person, code = await person_and_code(env.client)
    device = await pair(env.client, code)
    response = await env.client.post(
        "/client/v1/messages",
        json={"request_id": str(uuid4()), "text": "remember my App message"},
        headers=bearer(device["token"]),
    )
    await drain(env)
    async with database.sessions() as session:
        event = await session.get(ChatEventModel, response.json()["data"]["event_id"])
        job = await session.scalar(
            select(MemoryJobModel).where(MemoryJobModel.event_id == event.id)
        )
        assert job is not None and job.canonical_person_id == person
        source = dict(
            origin="user_message",
            generation=1,
            trigger_event_id=event.id,
            actor_person_id=person,
            presence_id=event.ingress_presence_id,
            bot_user_id=event.bot_user_id,
        )
    recovered = await recover_source(
        database, event.canonical_conversation_id, source, request_id="original-work"
    )
    original = await EventLedgerRepository(database).get_event(event.id)
    assert recovered.platform == "app" and recovered.inbound(original).platform == "app"
    assert recovered.actor_person_id == person and recovered.event_id == event.id


async def test_qq_delivery_in_flight_during_transfer_keeps_original_recipient_history(
    database, tmp_path, monkeypatch
):
    from tests.support.social_identity_cases import social_env
    from tests.unit.test_client_identity import code_for

    from yuki.conversation.canonical_db_models import CanonicalConversationModel
    from yuki.identity.canonical_repository import find_identity_binding

    env = await social_env(database, tmp_path)
    assert await env.router.cas_takeover_person(env.person) == "taken"
    service, target, _code = await code_for(database, "1002")
    async with database.sessions() as session:
        original_binding = await find_identity_binding(session, "10001")
    call = env.bot.call_api

    async def transfer_during_send(action, **params):
        if action == "send_private_msg":
            await service.transfer_for_operator(
                original_binding.id, target, str(uuid4()), str(uuid4())
            )
        return await call(action, **params)

    monkeypatch.setattr(env.bot, "call_api", transfer_during_send)
    result = await env.service.execute(
        "send_message",
        {
            "target": {"kind": "person", "target_id": env.person},
            "text": "dispatched for original owner",
        },
        env.context,
    )
    assert result["status"] == "succeeded", result
    async with database.sessions() as session:
        event = await session.get(ChatEventModel, result["event_id"])
        conversation = await session.get(
            CanonicalConversationModel, event.canonical_conversation_id
        )
        current = await find_identity_binding(session, "10001")
        assert current.person_id == target and conversation.person_id == env.person
        assert conversation.identity_binding_id != current.id
        assert event.content == "dispatched for original owner"


async def test_messages_and_device_commands_share_person_request_namespace(app_env):
    env = app_env
    _, code = await person_and_code(env.client)
    device = await pair(env.client, code)
    message_id = str(uuid4())
    sent = await env.client.post(
        "/client/v1/messages",
        json={"request_id": message_id, "text": "original message"},
        headers=bearer(device["token"]),
    )
    assert sent.status_code == 200
    conflict = await env.client.post(
        "/client/v1/link-codes", json={"request_id": message_id}, headers=bearer(device["token"])
    )
    assert conflict.status_code == 409
    code_id = str(uuid4())
    code = await env.client.post(
        "/client/v1/link-codes", json={"request_id": code_id}, headers=bearer(device["token"])
    )
    assert code.status_code == 200
    conflict = await env.client.post(
        "/client/v1/messages",
        json={"request_id": code_id, "text": "different operation"},
        headers=bearer(device["token"]),
    )
    assert conflict.status_code == 409
    await drain(env)
    assert len(env.calls) == 2


async def test_app_execution_preserves_entry_and_uses_only_its_persons_real_grant(
    app_env, database, monkeypatch
):
    from yuki.automation.authority import PermissionLevel
    from yuki.automation.control_context import resolve_execution_identity
    from yuki.identity.canonical_repository import find_identity_binding

    env = app_env
    person, code = await person_and_code(env.client)
    device = await pair(env.client, code)
    principal = await env.identity.authenticate(device["token"])
    async with database.sessions() as session:
        superuser = await find_identity_binding(session, "9000")
        account, permission = await resolve_execution_identity(
            session,
            owner_id=person,
            conversation_id=principal.conversation_id,
        )
    assert account == principal.external_account_id and permission is PermissionLevel.USER
    await env.identity.transfer_for_operator(superuser.id, person, str(uuid4()), str(uuid4()))
    async with database.sessions() as session:
        _, permission = await resolve_execution_identity(
            session,
            owner_id=person,
            conversation_id=principal.conversation_id,
        )
    assert permission is PermissionLevel.USER
    from yuki.identity.db_models import CanonicalPersonModel

    async with database.immediate_session() as writer:
        owner = await writer.get(CanonicalPersonModel, person)
        owner.is_superuser = True
    async with database.sessions() as session:
        account, permission = await resolve_execution_identity(
            session,
            owner_id=person,
            conversation_id=principal.conversation_id,
        )
    assert account == principal.external_account_id and permission is PermissionLevel.SUPERUSER
    config = env.harness.processor._runtime_config
    snapshot = AsyncMock(wraps=config.snapshot)
    monkeypatch.setattr(config, "snapshot", snapshot)
    response = await env.client.post(
        "/client/v1/messages",
        json={"request_id": str(uuid4()), "text": "use the original App entry"},
        headers=bearer(device["token"]),
    )
    assert response.status_code == 200
    await drain(env)
    assert len(env.calls) == 2
    owners = [
        call.kwargs["user_id"] for call in snapshot.await_args_list if call.kwargs.get("user_id")
    ]
    assert owners and set(owners) == {person}


async def test_app_memory_read_scope_uses_canonical_owner_without_qq_alias(app_env, database):
    from yuki.memory.read_scope import MemoryReadScopeResolver

    env = app_env
    person, code = await person_and_code(env.client)
    device = await pair(env.client, code)
    principal = await env.identity.authenticate(device["token"])
    scope = await MemoryReadScopeResolver(database).authorized(person, private_scene=True)
    assert scope.requester_person_id == scope.current_private_person_id == principal.person_id


async def test_local_gateway_does_not_claim_qq_connectivity(app_env):
    assert app_env.gateway.registry.has_any_active()
    assert app_env.gateway.registry.has_any_active(platform="app")
    assert not app_env.gateway.registry.has_any_active(platform="qq")


async def test_app_text_does_not_execute_platform_control_commands(app_env, database):
    from yuki.conversation.canonical_db_models import CanonicalConversationModel

    env = app_env
    _, code = await person_and_code(env.client)
    device = await pair(env.client, code)
    response = await env.client.post(
        "/client/v1/messages",
        json={"request_id": str(uuid4()), "text": "/ai new"},
        headers=bearer(device["token"]),
    )
    assert response.status_code == 200
    await drain(env)
    async with database.sessions() as session:
        conversation = await session.get(
            CanonicalConversationModel, response.json()["data"]["conversation_id"]
        )
        assert conversation.generation == 1
    assert len(env.calls) == 2
