"""Personal and operator credentials have separate HTTP authority."""

import asyncio
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select
from tests.unit.test_admin_api import (
    READER,
    TOKEN,
    command,
)
from tests.unit.test_admin_api import (
    admin as admin_fixture,
)

from yuki.admin_api.http import attach_admin_api
from yuki.client_api.db_models import ClientDeviceModel
from yuki.client_api.http import ClientApi, attach_client_api
from yuki.conversation.canonical_db_models import CanonicalConversationModel
from yuki.identity.canonical_repository import find_identity_binding
from yuki.identity.db_models import IdentityBindingModel

admin = admin_fixture


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
async def personal(admin):
    _, api, *_ = admin
    app = FastAPI()
    attach_admin_api(app, lambda: api)
    attach_client_api(app, lambda: ClientApi(api.identities))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client


async def person_and_code(client):
    created = await client.post("/admin/v1/persons", json=command(), headers=bearer(TOKEN))
    person = created.json()["data"]["resource_id"]
    request = {"request_id": str(uuid4())}
    response = await client.post(
        f"/admin/v1/persons/{person}/client-pair", json=request, headers=bearer(TOKEN)
    )
    assert response.status_code == 200, response.text
    return person, response.json()["data"]["code"]


async def pair(client, code, label="device"):
    response = await client.post("/client/v1/pair", json={"code": code, "device_label": label})
    assert response.status_code == 200, response.text
    return response.json()["data"]


async def test_no_qq_needed_and_credentials_cannot_cross_http_authorities(personal):
    client = personal
    person, code = await person_and_code(client)
    forbidden = await client.post(
        "/client/v1/pair", json={"code": code, "device_label": "A"}, headers=bearer(TOKEN)
    )
    assert forbidden.status_code == 401
    first = await pair(client, code, "computer")
    assert (await client.get("/client/v1/me", headers=bearer(TOKEN))).status_code == 401
    assert (await client.get("/admin/v1/health", headers=bearer(first["token"]))).status_code == 401
    assert (await client.get("/client/v1/me")).status_code == 401
    me = await client.get("/client/v1/me", headers=bearer(first["token"]))
    assert me.json()["data"]["person_id"] == person
    assert [binding["platform"] for binding in me.json()["data"]["bindings"]] == ["app"]
    forged = await client.post(
        "/client/v1/link-codes",
        headers=bearer(first["token"]),
        json={"person_id": str(uuid4()), "token": first["token"]},
    )
    assert forged.status_code == 422 and first["token"] not in forged.text
    ignored = await client.get(f"/client/v1/me?person_id={uuid4()}", headers=bearer(first["token"]))
    assert ignored.json()["data"]["person_id"] == person
    reused = await client.post("/client/v1/pair", json={"code": code, "device_label": "reused"})
    assert reused.status_code == 401 and code not in reused.text


async def test_two_device_pairing_and_personal_revoke_are_isolated(personal):
    client = personal
    _, code = await person_and_code(client)
    first = await pair(client, code, "computer")
    issued = await client.post("/client/v1/link-codes", headers=bearer(first["token"]))
    second = await pair(client, issued.json()["data"]["code"], "phone")
    a = (await client.get("/client/v1/me", headers=bearer(first["token"]))).json()["data"]
    b = (await client.get("/client/v1/me", headers=bearer(second["token"]))).json()["data"]
    assert a["conversation_id"] == b["conversation_id"]
    _, other_code = await person_and_code(client)
    other = await pair(client, other_code)
    denied = await client.delete(
        f"/client/v1/devices/{first['device_id']}", headers=bearer(other["token"])
    )
    missing = await client.delete(f"/client/v1/devices/{uuid4()}", headers=bearer(other["token"]))
    assert denied.status_code == missing.status_code == 404 and denied.json() == missing.json()
    revoked = await client.delete(
        f"/client/v1/devices/{first['device_id']}", headers=bearer(second["token"])
    )
    assert revoked.status_code == 200
    assert (await client.get("/client/v1/me", headers=bearer(first["token"]))).status_code == 401
    assert (await client.get("/client/v1/me", headers=bearer(second["token"]))).status_code == 200


async def test_concurrent_pair_accepts_a_code_once(personal, database):
    person, code = await person_and_code(personal)
    responses = await asyncio.gather(
        *(
            personal.post(
                "/client/v1/pair",
                json={
                    "code": code,
                    "device_label": label,
                },
            )
            for label in ("computer", "phone")
        )
    )
    assert sorted(response.status_code for response in responses) == [200, 401]
    async with database.read_session() as session:
        assert (
            await session.scalar(
                select(func.count(ClientDeviceModel.id)).where(
                    ClientDeviceModel.person_id == person
                )
            )
            == 1
        )
        assert (
            await session.scalar(
                select(func.count(CanonicalConversationModel.id)).where(
                    CanonicalConversationModel.person_id == person
                )
            )
            == 1
        )


async def test_operator_transfer_replay_and_app_binding_refusal(personal, database):
    client = personal
    target, code = await person_and_code(client)
    device = await pair(client, code)
    async with database.read_session() as session:
        original = await find_identity_binding(session, "1001")
        original_id, owner = original.id, original.person_id
        app = await session.scalar(
            select(IdentityBindingModel).where(
                IdentityBindingModel.person_id == target, IdentityBindingModel.platform == "app"
            )
        )
        app_id = app.id
    request = {"request_id": str(uuid4()), "to_person": target}
    denied = await client.post(
        f"/admin/v1/bindings/{original_id}/transfer", json=request, headers=bearer(READER)
    )
    assert denied.status_code == 403
    response = await client.post(
        f"/admin/v1/bindings/{original_id}/transfer", json=request, headers=bearer(TOKEN)
    )
    assert response.status_code == 200, response.text
    replay = await client.post(
        f"/admin/v1/bindings/{original_id}/transfer", json=request, headers=bearer(TOKEN)
    )
    assert replay.json()["data"]["replay"]
    assert replay.json()["data"]["binding_id"] == response.json()["data"]["binding_id"]
    conflict = await client.post(
        f"/admin/v1/bindings/{original_id}/transfer",
        json={**request, "to_person": owner},
        headers=bearer(TOKEN),
    )
    assert conflict.status_code == 409
    disabled = await client.post(
        f"/admin/v1/bindings/{original_id}/disable",
        json=command(2),
        headers=bearer(TOKEN),
    )
    assert (
        disabled.status_code == 200
        and disabled.json()["data"]["effective_state"]["status"] == "ended"
    )
    prohibited = await client.post(
        f"/admin/v1/bindings/{app_id}/transfer",
        json={"request_id": str(uuid4()), "to_person": owner},
        headers=bearer(TOKEN),
    )
    assert prohibited.status_code == 409
    assert (await client.get("/client/v1/me", headers=bearer(device["token"]))).status_code == 200


async def test_operator_forget_works_without_platform_account_and_replays(personal, database):
    person, code = await person_and_code(personal)
    device = await pair(personal, code)
    request = {"request_id": str(uuid4())}
    denied = await personal.post(
        f"/admin/v1/persons/{person}/forget", json=request, headers=bearer(READER)
    )
    assert denied.status_code == 403
    result = await personal.post(
        f"/admin/v1/persons/{person}/forget", json=request, headers=bearer(TOKEN)
    )
    assert result.status_code == 200, result.text
    replay = await personal.post(
        f"/admin/v1/persons/{person}/forget", json=request, headers=bearer(TOKEN)
    )
    assert replay.status_code == 200 and replay.json()["data"]["replay"]
    assert (await personal.get("/client/v1/me", headers=bearer(device["token"]))).status_code == 401
    async with database.read_session() as session:
        assert await session.get(ClientDeviceModel, device["device_id"]) is None


async def test_pair_waits_for_actual_writer_without_consuming_code_twice(personal, database):
    import sqlite3

    from sqlalchemy import event

    person, code = await person_and_code(personal)
    writing = asyncio.Event()

    def before_write(_connection, _cursor, statement, _parameters, _context, _executemany):
        if statement.lstrip().upper().startswith("BEGIN IMMEDIATE"):
            writing.set()

    event.listen(database.engine.sync_engine, "before_cursor_execute", before_write)
    with sqlite3.connect(database.url.removeprefix("sqlite+aiosqlite:///")) as blocker:
        blocker.execute("BEGIN IMMEDIATE")
        request = asyncio.create_task(
            personal.post("/client/v1/pair", json={"code": code, "device_label": "busy"})
        )
        try:
            await writing.wait()
            blocker.rollback()
            response = await request
        finally:
            event.remove(database.engine.sync_engine, "before_cursor_execute", before_write)
    assert response.status_code == 200, response.text
    device = response.json()["data"]
    reused = await personal.post("/client/v1/pair", json={"code": code, "device_label": "retry"})
    assert reused.status_code == 401
    assert (await personal.get("/client/v1/me", headers=bearer(device["token"]))).json()["data"][
        "person_id"
    ] == person


@pytest.mark.parametrize("error_kind", ["sqlalchemy", "client", "identity"])
async def test_lost_pair_commit_ack_is_unknown_and_code_is_still_one_time(
    personal, database, monkeypatch, error_kind
):
    import sqlite3

    from sqlalchemy.exc import OperationalError
    from sqlalchemy.ext.asyncio import AsyncSession

    from yuki.client_api.errors import ClientError
    from yuki.identity.errors import CanonicalIdentityError

    person, code = await person_and_code(personal)
    commit = AsyncSession.commit

    async def lost_ack(session):
        await commit(session)
        if error_kind == "client":
            raise ClientError("unauthenticated")
        if error_kind == "identity":
            raise CanonicalIdentityError("account_observation_changed")
        raise OperationalError(
            "COMMIT", {}, sqlite3.OperationalError("lost commit acknowledgement")
        )

    with monkeypatch.context() as scoped:
        scoped.setattr(AsyncSession, "commit", lost_ack)
        response = await personal.post(
            "/client/v1/pair", json={"code": code, "device_label": "lost"}
        )
    assert response.status_code == 500 and response.json()["problem"]["code"] == "unknown"
    retry = await personal.post("/client/v1/pair", json={"code": code, "device_label": "retry"})
    assert retry.status_code == 401
    async with database.read_session() as session:
        assert (
            await session.scalar(
                select(func.count(ClientDeviceModel.id)).where(
                    ClientDeviceModel.person_id == person
                )
            )
            == 1
        )
