"""Computer authorization uses the original Person, without a chat session."""

import argparse
import asyncio
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import event, func, select
from tests.support.client_identity import client_identity_service
from tests.unit.test_admin_api import admin as admin_fixture
from tests.unit.test_client_api import (
    bearer,
    pair,
    person_and_code,
)
from tests.unit.test_client_api import personal as personal_fixture

from yuki.admin_api.cli import add_admin_parser, admin_command
from yuki.client_api.db_models import APP_PLATFORM, ClientDeviceModel, ClientLinkCodeModel
from yuki.client_api.errors import ClientError
from yuki.conversation.canonical_db_models import CanonicalConversationModel
from yuki.external_agents.identity import ConnectorIdentityService
from yuki.identity.canonical_repository import find_identity_binding
from yuki.identity.credentials import require_person
from yuki.identity.db_models import CanonicalPersonModel, IdentityBindingModel
from yuki.identity.person_identity import merge_persons

admin = admin_fixture
personal = personal_fixture


async def connector_for(database, account="1001"):
    chat = client_identity_service(database)
    async with database.sessions() as session:
        binding = await find_identity_binding(session, account)
        person_id = binding.person_id
    code = await chat.issue_for_operator(person_id, str(uuid4()), str(uuid4()), purpose="connector")
    connector = ConnectorIdentityService(database)
    device = await connector.pair(code["code"], "work computer")
    return chat, connector, person_id, device


async def test_connector_pair_does_not_create_chat_entry_and_credentials_cannot_cross(database):
    chat, connector, person_id, device = await connector_for(database)
    principal = await connector.authenticate(device["token"])
    assert principal.device_id == device["device_id"]
    assert principal.person_id == principal.source_person_id == person_id
    async with database.sessions() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(IdentityBindingModel)
                .where(
                    IdentityBindingModel.person_id == person_id,
                    IdentityBindingModel.platform == APP_PLATFORM,
                )
            )
            == 0
        )
        assert (
            await session.scalar(
                select(func.count())
                .select_from(CanonicalConversationModel)
                .where(
                    CanonicalConversationModel.person_id == person_id,
                    CanonicalConversationModel.kind == "private",
                )
            )
            == 0
        )
        stored = await session.get(ClientDeviceModel, device["device_id"])
        assert stored.person_id == person_id and stored.purpose == "connector"
        assert stored.token_hash != device["token"]
    with pytest.raises(ClientError, match="unauthenticated"):
        await chat.authenticate(device["token"])
    issued = await chat.issue_for_operator(person_id, str(uuid4()), str(uuid4()))
    with pytest.raises(ClientError, match="unauthenticated"):
        await connector.pair(issued["code"], "wrong purpose")
    app = await chat.pair(issued["code"], "chat")
    with pytest.raises(ClientError, match="unauthenticated"):
        await connector.authenticate(app["token"])
    issued = await chat.issue_for_operator(
        person_id, str(uuid4()), str(uuid4()), purpose="connector"
    )
    with pytest.raises(ClientError, match="unauthenticated"):
        await chat.pair(issued["code"], "wrong purpose")
    await connector.pair(issued["code"], "right purpose")
    with pytest.raises(ClientError, match="unauthenticated"):
        await connector.pair(issued["code"], "reused")


async def test_device_person_survives_account_transfer_and_merge_preserves_source(database):
    chat, connector, source_id, device = await connector_for(database)
    async with database.immediate_session() as session:
        source = await find_identity_binding(session, "1001")
        other = await find_identity_binding(session, "1002")
        target_id = other.person_id
        await chat.transfer_binding(session, source, target_id, now=datetime.now(UTC))
    assert (await connector.authenticate(device["token"])).person_id == source_id
    async with database.immediate_session() as session:
        await merge_persons(session, source_id, target_id)
    principal = await connector.authenticate(device["token"])
    assert principal.person_id == target_id and principal.source_person_id == source_id
    async with database.sessions() as session:
        assert (await session.get(ClientDeviceModel, device["device_id"])).person_id == source_id
        assert await require_person(session, source_id) == target_id
    async with database.immediate_session() as session:
        (await session.get(CanonicalPersonModel, source_id)).enabled = False
    with pytest.raises(ClientError, match="unauthenticated"):
        await connector.authenticate(device["token"])
    async with database.immediate_session() as session:
        (await session.get(CanonicalPersonModel, source_id)).enabled = True
        (await session.get(CanonicalPersonModel, target_id)).enabled = False
    with pytest.raises(ClientError, match="unauthenticated"):
        await connector.authenticate(device["token"])


async def test_expiration_revocation_and_personal_control_are_independent(database):
    chat, connector, person_id, device = await connector_for(database)
    issued = await chat.issue_for_operator(
        person_id, str(uuid4()), str(uuid4()), purpose="connector"
    )
    async with database.immediate_session() as session:
        (await session.get(ClientLinkCodeModel, issued["code_id"])).expires_at = datetime.now(
            UTC
        ) - timedelta(seconds=1)
    with pytest.raises(ClientError, match="unauthenticated"):
        await connector.pair(issued["code"], "expired")
    chat_code = await chat.issue_for_operator(person_id, str(uuid4()), str(uuid4()))
    app = await chat.pair(chat_code["code"], "chat")
    principal = await chat.authenticate(app["token"])
    other_chat, _, _, other_device = await connector_for(database, "1002")
    with pytest.raises(ClientError, match="not_found"):
        await other_chat.revoke(principal, other_device["device_id"], str(uuid4()))
    await chat.revoke(principal, device["device_id"], str(uuid4()))
    async with database.sessions() as session:
        with pytest.raises(ClientError, match="unauthenticated"):
            await connector.require_device(session, device["device_id"])
    with pytest.raises(ClientError, match="unauthenticated"):
        await connector.authenticate(device["token"])
    assert (await chat.authenticate(app["token"])).person_id == person_id


async def test_accountless_person_can_pair_and_authentication_does_not_write(database):
    person_id = str(uuid4())
    now = datetime.now(UTC)
    async with database.immediate_session() as session:
        session.add(CanonicalPersonModel(id=person_id, created_at=now, updated_at=now))
    chat = client_identity_service(database)
    code = await chat.issue_for_operator(person_id, str(uuid4()), str(uuid4()), purpose="connector")
    connector = ConnectorIdentityService(database)
    device = await connector.pair(code["code"], "headless computer")
    writes = []

    def record_write(_connection, _cursor, statement, _parameters, _context, _executemany):
        if statement.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")):
            writes.append(statement)

    event.listen(database.engine.sync_engine, "before_cursor_execute", record_write)
    try:
        assert (await connector.authenticate(device["token"])).person_id == person_id
    finally:
        event.remove(database.engine.sync_engine, "before_cursor_execute", record_write)
    assert writes == []
    async with database.sessions() as session:
        assert (
            await session.scalar(
                select(IdentityBindingModel.id).where(IdentityBindingModel.person_id == person_id)
            )
            is None
        )


async def test_concurrent_pair_consumes_one_code_only_once(database):
    chat, connector, person_id, _device = await connector_for(database)
    issued = await chat.issue_for_operator(
        person_id, str(uuid4()), str(uuid4()), purpose="connector"
    )
    results = await asyncio.gather(
        connector.pair(issued["code"], "first contender"),
        connector.pair(issued["code"], "second contender"),
        return_exceptions=True,
    )
    successes = [result for result in results if isinstance(result, dict)]
    failures = [result for result in results if isinstance(result, ClientError)]
    assert len(successes) == len(failures) == 1
    assert failures[0].code == "unauthenticated"
    assert (await connector.authenticate(successes[0]["token"])).person_id == person_id


async def test_pausing_chat_entry_does_not_revoke_existing_computer(database):
    chat, connector, person_id, device = await connector_for(database)
    code = await chat.issue_for_operator(person_id, str(uuid4()), str(uuid4()))
    app = await chat.pair(code["code"], "chat")
    principal = await chat.authenticate(app["token"])
    async with database.immediate_session() as session:
        (await session.get(IdentityBindingModel, principal.binding_id)).status = "disabled"
    assert (await connector.authenticate(device["token"])).device_id == device["device_id"]


async def test_personal_api_explicitly_grants_connector_and_replay_cannot_change_purpose(
    personal, admin
):
    person_id, code = await person_and_code(personal)
    app = await pair(personal, code)
    original = {"request_id": str(uuid4()), "purpose": "connector"}
    issued = await personal.post(
        "/client/v1/link-codes", headers=bearer(app["token"]), json=original
    )
    assert issued.status_code == 200, issued.text
    result = issued.json()["data"]
    assert result["purpose"] == "connector"
    replay = await personal.post(
        "/client/v1/link-codes", headers=bearer(app["token"]), json=original
    )
    assert replay.json()["data"]["replay"] and replay.json()["data"]["code"] is None
    changed = await personal.post(
        "/client/v1/link-codes", headers=bearer(app["token"]), json={**original, "purpose": "chat"}
    )
    assert changed.status_code == 409
    rejected = await personal.post(
        "/client/v1/pair", json={"code": result["code"], "device_label": "computer"}
    )
    assert rejected.status_code == 401
    connector = ConnectorIdentityService(admin[1].identities.database)
    device = await connector.pair(result["code"], "computer")
    assert (await connector.authenticate(device["token"])).person_id == person_id
    assert (await personal.get("/client/v1/me", headers=bearer(device["token"]))).status_code == 401
    revoked = await personal.delete(
        f"/client/v1/devices/{device['device_id']}", headers=bearer(app["token"])
    )
    assert revoked.status_code == 200, revoked.text
    with pytest.raises(ClientError, match="unauthenticated"):
        await connector.authenticate(device["token"])


async def test_operator_cli_explicit_connector_purpose_reaches_real_api(admin, monkeypatch, capsys):
    client, api, *_ = admin

    @asynccontextmanager
    async def connected(_settings):
        yield client

    monkeypatch.setattr("yuki.admin_api.cli.admin_client", connected)
    parser = argparse.ArgumentParser()
    add_admin_parser(parser.add_subparsers(dest="command", required=True))
    async with api.identities.database.sessions() as session:
        person_id = (await find_identity_binding(session, "1001")).person_id
    args = parser.parse_args(
        ["admin", "client", "pair", "--person", person_id, "--purpose", "connector"]
    )
    assert await admin_command(None, args) == 0
    issued = json.loads(capsys.readouterr().out)
    assert issued["purpose"] == "connector"
    connector = ConnectorIdentityService(api.identities.database)
    device = await connector.pair(issued["code"], "computer")
    assert (await connector.authenticate(device["token"])).person_id == person_id
