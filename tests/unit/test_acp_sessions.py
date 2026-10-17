"""ACP uses the original person and independent canonical entry sessions."""

import asyncio
import hashlib
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text
from tests.unit.test_admin_api import admin as admin_fixture
from tests.unit.test_app_messages import app_env as app_fixture
from tests.unit.test_client_works import owner

from qq_ai_bot.client_api.errors import ClientError
from qq_ai_bot.client_api.identity import ClientIdentityService
from qq_ai_bot.client_api.requests import personal_command
from qq_ai_bot.client_api.sessions import ClientSessions
from qq_ai_bot.conversation.canonical_db_models import (
    CanonicalConversationModel,
    ControlCommandReceiptModel,
)
from qq_ai_bot.conversation.hydrate import bump_canonical_generation
from qq_ai_bot.conversation.scope_repository import ConversationScopeRepository
from qq_ai_bot.domain.conversations import ConversationScope
from qq_ai_bot.identity.canonical_repository import find_identity_binding
from qq_ai_bot.identity.db_models import CanonicalPersonModel, IdentityBindingModel
from qq_ai_bot.identity.person_identity import merge_persons
from qq_ai_bot.persistence.people_repository import PeopleRepository
from qq_ai_bot.persistence.scoped_event_uow import ScopedEventLedgerUnitOfWork

app_env = app_fixture
admin = admin_fixture


async def paired(database, account="1001"):
    identity = ClientIdentityService(database)
    async with database.sessions() as session:
        binding = await find_identity_binding(session, account)
    code = await identity.issue_for_operator(binding.person_id, str(uuid4()), str(uuid4()))
    device = await identity.pair(code["code"], "ACP fixture")
    return identity, device, await identity.authenticate(device["token"])


async def test_new_sessions_keep_one_binding_and_do_not_rebuild_other_context(database):
    identity, device, base = await paired(database)
    service = ClientSessions(identity)
    async with database.sessions() as session:
        people = await session.scalar(select(func.count(CanonicalPersonModel.id)))
    request_id = str(uuid4())
    first, replay = await asyncio.gather(
        service.create(base, request_id), service.create(base, request_id)
    )
    assert first["session_id"] == replay["session_id"]
    assert sorted((first["replay"], replay["replay"])) == [False, True]
    second = await service.create(base, str(uuid4()))
    assert first["binding_id"] == second["binding_id"] != base.binding_id
    assert first["session_id"] != second["session_id"] != base.conversation_id
    async with database.sessions() as session:
        assert await session.scalar(select(func.count(CanonicalPersonModel.id))) == people
        rows = list(
            await session.scalars(
                select(CanonicalConversationModel).where(
                    CanonicalConversationModel.identity_binding_id == first["binding_id"]
                )
            )
        )
        assert len(rows) == 2 and all(row.generation == 1 for row in rows)
        assert len({row.thread_id for row in rows}) == 2
        assert (await session.get(CanonicalConversationModel, base.conversation_id)).generation == 1
    assert (
        await identity.authenticate(device["token"], conversation_id=first["session_id"])
    ).platform == "acp"


async def test_acp_threads_keep_events_and_generation_separate_from_app(database):
    identity, device, base = await paired(database)
    service = ClientSessions(identity)
    sessions = [await service.create(base, str(uuid4())) for _ in range(2)]
    principals = [base] + [
        await identity.authenticate(device["token"], conversation_id=item["session_id"])
        for item in sessions
    ]
    writer = ScopedEventLedgerUnitOfWork(database)
    scopes, events = [], []
    request_id = str(uuid4())
    for index, principal in enumerate(principals):
        scope = ConversationScope.private(
            "yuki-acp" if principal.platform == "acp" else "yuki-app",
            principal.external_account_id,
            platform=principal.platform,
            presence_id=principal.presence_id,
            thread_id=principal.thread_id,
        )
        scopes.append(scope)
        event = await writer.append(
            scope=scope,
            platform_message_id=principal.message_key(request_id),
            sender_user_id=principal.external_account_id,
            direction="inbound",
            content=f"original session {index}",
        )
        events.append(event.event)
        assert event.event.canonical_conversation_id == principal.conversation_id
    repository = ConversationScopeRepository(database)
    for index, scope in enumerate(scopes):
        snapshot = await repository.load_prompt_snapshot(scope)
        assert [event.content for event in snapshot.raw_events] == [f"original session {index}"]
    async with database.immediate_session() as session:
        await bump_canonical_generation(
            session, principals[1].conversation_id, event_id=events[1].id, force=True
        )
    assert [(await repository.get(scope)).generation for scope in scopes] == [1, 2, 1]


async def test_scoped_commands_share_uuid_only_inside_the_selected_conversation(database):
    identity, device, base = await paired(database)
    service = ClientSessions(identity)
    entries = [await service.create(base, str(uuid4())) for _ in range(2)]
    principals = [base] + [
        await identity.authenticate(device["token"], conversation_id=entry["session_id"])
        for entry in entries
    ]
    request_id = str(uuid4())
    results = [await identity.issue_for_device(principal, request_id) for principal in principals]
    assert len({result["code_id"] for result in results}) == 3
    for principal, result in zip(principals, results, strict=True):
        repeated = await identity.issue_for_device(principal, request_id)
        assert repeated["code_id"] == result["code_id"] and repeated["replay"]
        async with database.sessions() as session:
            receipt = await personal_command(
                session,
                principal.request_person_id,
                request_id,
                conversation_id=principal.request_conversation_id,
            )
            assert receipt.result_resource_id == result["code_id"]


async def test_session_load_checks_entry_person_and_keeps_original_binding_after_merge(database):
    identity, first, base = await paired(database)
    service = ClientSessions(identity)
    entry = await service.create(base, str(uuid4()))
    _, second, other = await paired(database, "1002")
    with pytest.raises(ClientError, match="not_found"):
        await service.load(other, entry["session_id"])
    with pytest.raises(ClientError, match="not_found"):
        await identity.authenticate(first["token"], conversation_id=base.conversation_id)
    with pytest.raises(ClientError, match="not_found"):
        await identity.authenticate(
            first["token"], binding_id=base.binding_id, conversation_id=entry["session_id"]
        )
    async with database.immediate_session() as session:
        await merge_persons(session, base.person_id, other.person_id, now=datetime.now(UTC))
    current = await identity.authenticate(first["token"], conversation_id=entry["session_id"])
    assert current.person_id == other.person_id
    assert current.source_person_id == current.request_person_id == base.person_id
    assert current.binding_id == entry["binding_id"]
    assert (await service.load(other, entry["session_id"]))["binding_id"] == entry["binding_id"]
    await identity.revoke(other, first["device_id"], str(uuid4()))
    with pytest.raises(ClientError, match="unauthenticated"):
        await identity.authenticate(first["token"], conversation_id=entry["session_id"])
    assert (await identity.authenticate(second["token"])).person_id == other.person_id


@pytest.mark.parametrize("disabled", ["source", "root", "binding"])
async def test_session_revalidation_keeps_person_disable_and_binding_read_semantics(
    database, disabled
):
    identity, device, base = await paired(database)
    service = ClientSessions(identity)
    entry = await service.create(base, str(uuid4()))
    principal = await identity.authenticate(device["token"], conversation_id=entry["session_id"])
    if disabled == "root":
        _, _, other = await paired(database, "1002")
        async with database.immediate_session() as session:
            await merge_persons(session, base.person_id, other.person_id, now=datetime.now(UTC))
    async with database.immediate_session() as session:
        if disabled == "binding":
            (await session.get(IdentityBindingModel, entry["binding_id"])).status = "disabled"
        else:
            person_id = other.person_id if disabled == "root" else base.person_id
            (await session.get(CanonicalPersonModel, person_id)).enabled = False
    if disabled == "binding":
        assert (await service.load(base, entry["session_id"]))["session_id"] == entry["session_id"]
        with pytest.raises(ClientError, match="precondition_failed"):
            await service.create(base, str(uuid4()))
    else:
        async with database.sessions() as session:
            with pytest.raises(ClientError, match="unauthenticated"):
                await identity.require_principal(session, principal)


async def test_forget_removes_all_acp_scopes_before_their_conversations(database):
    identity, device, base = await paired(database)
    entries = [await ClientSessions(identity).create(base, str(uuid4())) for _ in range(2)]
    request_id = str(uuid4())
    for entry in entries:
        principal = await identity.authenticate(
            device["token"], conversation_id=entry["session_id"]
        )
        await identity.issue_for_device(principal, request_id)
    assert await PeopleRepository(database).delete_person_id(base.person_id)
    async with database.sessions() as session:
        assert not list(
            await session.scalars(
                select(ControlCommandReceiptModel).where(
                    ControlCommandReceiptModel.principal_id == device["device_id"]
                )
            )
        )
        assert not list(await session.execute(text("PRAGMA foreign_key_check")))
    with pytest.raises(ClientError, match="unauthenticated"):
        await identity.authenticate(device["token"], conversation_id=entries[0]["session_id"])


async def test_upload_uuid_and_receipt_namespaces_are_independent(app_env, database):
    env = app_env
    device, _ = await owner(env, database)
    base = await env.identity.authenticate(device["token"])
    entries = [await ClientSessions(env.identity).create(base, str(uuid4())) for _ in range(2)]
    scopes = [None, *(entry["session_id"] for entry in entries)]
    request_id = str(uuid4())
    media = []
    for index, scope in enumerate(scopes):
        headers = {"Authorization": f"Bearer {device['token']}"}
        if scope is not None:
            headers["X-Yuki-Conversation"] = scope
        content = f"distinct original bytes {index}".encode()
        parameters = {
            "request_id": request_id,
            "sha256": hashlib.sha256(content).hexdigest(),
            "size": len(content),
            "filename": "original.txt",
            "kind": "file",
        }
        response = await env.client.post(
            "/client/v1/uploads", headers=headers, params=parameters, content=content
        )
        assert response.status_code == 200, response.text
        result = response.json()["data"]
        media.append(result["media_id"])
        repeated = await env.client.post(
            "/client/v1/uploads", headers=headers, params=parameters, content=b"not read again"
        )
        assert repeated.status_code == 200 and repeated.json()["data"]["replay"]
        assert repeated.json()["data"]["media_id"] == result["media_id"]
    assert len(set(media)) == 3


async def test_acp_history_does_not_replay_unconfirmed_raw_outbound(app_env, database):
    device, _ = await owner(app_env, database)
    base = await app_env.identity.authenticate(device["token"])
    entry = await ClientSessions(app_env.identity).create(base, str(uuid4()))
    principal = await app_env.identity.authenticate(
        device["token"], conversation_id=entry["session_id"]
    )
    scope = ConversationScope.private(
        "yuki-acp",
        principal.external_account_id,
        platform="acp",
        presence_id=principal.presence_id,
        conversation_id=principal.conversation_id,
        thread_id=principal.thread_id,
    )
    writer = ScopedEventLedgerUnitOfWork(database)
    inbound = await writer.append(
        scope=scope,
        platform_message_id="real-user",
        sender_user_id=principal.external_account_id,
        direction="inbound",
        content="Visible original user input",
    )
    await writer.append(
        scope=scope,
        platform_message_id="raw-without-social-receipt",
        sender_user_id="yuki-acp",
        direction="outbound",
        content="Unconfirmed output must not become ACP history",
    )
    response = await app_env.client.get(
        f"/client/v1/conversations/{entry['session_id']}/events",
        headers={
            "Authorization": f"Bearer {device['token']}",
            "X-Yuki-Conversation": entry["session_id"],
        },
    )
    assert response.status_code == 200, response.text
    assert [row["event_id"] for row in response.json()["data"]["items"]] == [inbound.event.id]
