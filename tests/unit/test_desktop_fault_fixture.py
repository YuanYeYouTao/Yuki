"""Desktop fault controls lose real ACKs and keep current identity/receipt semantics."""

import asyncio
import hashlib
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select
from tests.support.desktop_fault_fixture import DesktopFaultFixture
from tests.unit.test_admin_api import admin as admin_fixture
from tests.unit.test_app_messages import app_env as app_fixture
from tests.unit.test_app_messages import drain
from tests.unit.test_client_api import bearer, pair, person_and_code
from tests.unit.test_client_works import owner, seeded

from yuki.client_api.http import ClientApi, attach_client_api
from yuki.conversation.canonical_db_models import ControlCommandReceiptModel
from yuki.persistence.models import ChatEventModel, ConversationMediaItemModel

admin = admin_fixture
app_env = app_fixture


@pytest.fixture
async def faults(app_env, database, tmp_path):
    env = app_env
    device, person = await owner(env, database)
    other, code = await person_and_code(env.client)
    api = ClientApi(
        env.identity,
        messages=env.service,
        works=env.files.views.works,
        views=env.files.views,
        files=env.files,
    )
    runtime = SimpleNamespace(
        database=database,
        client_api=api,
        workspace_service=SimpleNamespace(store=env.store),
        social_service=env.social,
        conversation_media=env.media,
    )

    async def other_code():
        return {"code": code}

    fixture = DesktopFaultFixture(runtime, tmp_path, person, other_code, env.provider)
    app = FastAPI()
    attach_client_api(app, lambda: api)
    fixture.attach(app)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://desktop"
    ) as client:
        try:
            yield SimpleNamespace(
                client=client,
                fixture=fixture,
                api=api,
                env=env,
                database=database,
                device=device,
                person=person,
                other=other,
                headers=bearer(device["token"]),
            )
        finally:
            await fixture.close()


async def test_paused_me_reads_real_principal_and_other_code_keeps_separate_person(faults):
    f = faults
    assert (await f.client.post("/__test/pause-next-me")).status_code == 200
    pending = asyncio.create_task(f.client.get("/client/v1/me", headers=f.headers))
    try:
        await asyncio.wait_for(f.fixture.me_blocked.wait(), 5)
        assert not pending.done()
        assert (await f.client.get("/__test/me-blocked")).json() == {"blocked": True}
        await f.client.post("/__test/resume-me")
        response = await pending
        assert response.json()["data"]["person_id"] == f.person
        code = (await f.client.post("/__test/other-person-code")).json()["code"]
        other = await pair(f.client, code)
        principal = await f.env.identity.authenticate(other["token"])
        assert principal.person_id == f.other != f.person
        assert (await f.client.post("/__test/model-delay/0.01")).status_code == 200
        assert f.env.provider._delay_seconds == 0.01
    finally:
        await f.client.post("/__test/resume-me")
        await pending


async def test_lost_upload_ack_keeps_receipt_and_unavailable_expired_original_denies_read(faults):
    f = faults
    payload = b"actual original cross-device bytes"
    request_id = str(uuid4())
    await f.client.post("/__test/lose-next-upload-response")
    lost = await f.client.post(
        "/client/v1/uploads",
        params={
            "request_id": request_id,
            "sha256": hashlib.sha256(payload).hexdigest(),
            "size": len(payload),
            "filename": "original.txt",
            "kind": "file",
        },
        content=payload,
        headers=f.headers,
    )
    assert lost.status_code == 503
    recovered = await f.client.get(f"/client/v1/requests/{request_id}", headers=f.headers)
    receipt = recovered.json()["data"]
    assert receipt["found"] and receipt["operation_kind"] == "client.upload"
    media = receipt["result"]
    assert f.env.files.uploads.path(media["sha256"]).read_bytes() == payload
    assert (await f.client.get("/__test/uploads")).json() == {"requests": [request_id]}
    message = await f.client.post(
        "/client/v1/messages",
        headers=f.headers,
        json={
            "request_id": str(uuid4()),
            "text": "keep original",
            "attachments": [media["media_id"]],
        },
    )
    assert message.status_code == 200, message.text
    await drain(f.env)
    event_id = message.json()["data"]["event_id"]
    principal = await f.env.identity.authenticate(f.device["token"])
    path = f"/client/v1/conversations/{principal.conversation_id}/events/{event_id}/attachments/0"
    before = await f.client.get(path, headers=f.headers)
    assert before.status_code == 200 and before.content == payload
    assert (await f.client.post(f"/__test/expire-attachments/{event_id}")).status_code == 200
    assert (await f.client.get(path, headers=f.headers)).status_code == 404
    retained = list((f.fixture.root / "expired-originals").glob("*.blob"))
    assert len(retained) == 1 and retained[0].read_bytes() == payload
    unchanged = await f.client.get(f"/client/v1/requests/{request_id}", headers=f.headers)
    assert unchanged.json()["data"] == receipt
    async with f.database.read_session() as reader:
        assert (
            await reader.scalar(select(func.count()).select_from(ConversationMediaItemModel)) == 1
        )
        assert (
            await reader.scalar(
                select(func.count())
                .select_from(ControlCommandReceiptModel)
                .where(ControlCommandReceiptModel.operation_kind == "client.upload")
            )
            == 1
        )


async def test_lost_message_ack_and_artifacts_keep_original_send_and_immutable_versions(faults):
    f = faults
    request_id = str(uuid4())
    await f.client.post("/__test/lose-next-response")
    response = await f.client.post(
        "/client/v1/messages",
        json={"request_id": request_id, "text": "actual source", "attachments": []},
        headers=f.headers,
    )
    assert response.status_code == 503
    await drain(f.env)
    original = await f.client.get(f"/client/v1/requests/{request_id}", headers=f.headers)
    assert original.json()["data"]["found"]
    response = await f.client.post("/__test/artifacts?long_names=true")
    assert response.status_code == 200, response.text
    items = response.json()
    assert len(items) == 5
    assert items[0]["artifact_id"] == items[-1]["artifact_id"]
    assert items[0]["name"] == items[1]["name"] == "研究Data_" * 5 + "最終.md"
    assert items[0]["artifact_id"] != items[1]["artifact_id"]
    library = (await f.client.get("/client/v1/artifacts?limit=100", headers=f.headers)).json()[
        "data"
    ]
    assert len(library["items"]) == 5
    metadata, content = f.env.store.read_bytes(items[0]["artifact_id"])
    assert metadata["immutable"] and metadata["sha256"] == items[0]["sha256"]
    assert content == b"# First artifact\n\nOriginal immutable content.\n"
    async with f.database.read_session() as reader:
        assert (
            await reader.scalar(
                select(func.count())
                .select_from(ChatEventModel)
                .where(ChatEventModel.content == "actual source")
            )
            == 1
        )


async def test_disconnect_closes_real_client_stream_without_stopping_authority_subscriptions(
    faults,
):
    f = faults
    internal = f.database.commit_hints.subscribe()
    principal = await f.env.identity.authenticate(f.device["token"])
    stream = f.api.views.stream(principal, active=True)
    try:
        assert "event: sync" in await anext(stream)
        pending = asyncio.create_task(anext(stream))
        await f.client.post("/__test/disconnect")
        with pytest.raises(StopAsyncIteration):
            await pending
        assert internal in f.database.commit_hints.subscribers
        f.database.commit_hints.publish()
        assert await internal.get() is True
    finally:
        await stream.aclose()
        f.database.commit_hints.unsubscribe(internal)


async def test_work_loss_is_armed_by_control_then_discards_only_real_cancel_ack(faults):
    f = faults
    work = (await seeded(f.env, f.database, state="waiting_user")).row
    armed = await f.client.post("/__test/lose-next-work-response/cancel")
    assert armed.status_code == 200
    request_id = str(uuid4())
    response = await f.client.post(
        f"/client/v1/works/{work['id']}/cancel",
        headers=f.headers,
        json={"request_id": request_id, "expected_revision": work["revision"]},
    )
    assert response.status_code == 503, response.text
    receipt = await f.client.get(f"/client/v1/requests/{request_id}", headers=f.headers)
    assert receipt.status_code == 200, receipt.text
    data = receipt.json()["data"]
    assert data["found"] and data["operation_kind"] == "client.work_cancel"
    current = await f.client.get(f"/client/v1/works/{work['id']}", headers=f.headers)
    assert current.json()["data"]["state"] == "cancelled"


async def test_lost_delete_hint_closes_only_client_stream_and_reconnect_reads_real_deletion(faults):
    f = faults
    message = await f.client.post(
        "/client/v1/messages",
        headers=f.headers,
        json={"request_id": str(uuid4()), "text": "real source to retire", "attachments": []},
    )
    assert message.status_code == 200, message.text
    await drain(f.env)
    event_id = message.json()["data"]["event_id"]
    principal = await f.env.identity.authenticate(f.device["token"])
    stream = f.api.views.stream(principal, active=True)
    internal = None
    try:
        assert "event: sync" in await anext(stream)
        internal = f.database.commit_hints.subscribe()
        response = await f.client.delete(f"/__test/events/{event_id}?lose_hint=true")
        assert response.status_code == 200, response.text
        assert response.json() == {
            "deleted": event_id,
            "lost_hint": True,
            "client_streams_disconnected": 1,
        }
        with pytest.raises(StopAsyncIteration):
            await anext(stream)
        assert internal in f.database.commit_hints.subscribers
        assert await internal.get() is True
        fresh = f.api.views.stream(principal, active=True)
        try:
            assert "event: sync" in await anext(fresh)
            page = await f.api.views.events(principal, principal.conversation_id)
            assert all(item["event_id"] != event_id for item in page["items"])
        finally:
            await fresh.aclose()
    finally:
        await stream.aclose()
        if internal is not None:
            f.database.commit_hints.unsubscribe(internal)
