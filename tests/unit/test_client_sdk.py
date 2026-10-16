"""Persistent client requests recover transport uncertainty without new IDs."""

from uuid import uuid4

import httpx
import pytest
from tests.unit.test_admin_api import admin as admin_fixture
from tests.unit.test_app_messages import app_env as app_fixture
from tests.unit.test_app_messages import drain
from tests.unit.test_client_api import bearer, pair, person_and_code
from tests.unit.test_client_works import owner

from qq_ai_bot.client_sdk import ClientProblem, Outbox, PersonalClient

admin = admin_fixture
app_env = app_fixture


class DropAcknowledgement(httpx.AsyncBaseTransport):
    def __init__(self, app, resource="messages"):
        self.inner = httpx.ASGITransport(app=app)
        self.drop = True
        self.posts = 0
        self.resource = resource

    async def handle_async_request(self, request):
        response = await self.inner.handle_async_request(request)
        if request.method == "POST" and request.url.path.endswith("/" + self.resource):
            self.posts += 1
            if self.drop:
                self.drop = False
                await response.aread()
                raise httpx.ReadTimeout("test lost acknowledgement")
        return response


async def test_body_persists_before_dispatch_and_lost_ack_queries_original(
    app_env, database, tmp_path
):
    env = app_env
    device, _ = await owner(env, database)
    transport = DropAcknowledgement(env.app)
    async with httpx.AsyncClient(transport=transport) as http:
        box = Outbox(tmp_path / "device-a.sqlite")
        first = PersonalClient("http://test", device["token"], box, http=http)
        request = await first.prepare_message("original request")
        assert Outbox(box.path).get(request)["state"] == "draft" and not transport.posts
        with pytest.raises(ValueError, match="request_conflict"):
            await first.prepare_message("changed body", request_id=request)
        with pytest.raises(httpx.ReadTimeout):
            await first.submit(request)
        assert Outbox(box.path).get(request)["state"] == "submitted"
        await drain(env)
        # New process/client state uses the durable original body and queries before replay.
        restored = PersonalClient("http://test", device["token"], Outbox(box.path), http=http)
        outcome = await restored.submit(request)
        assert outcome["state"] == "result" and outcome["result"]["event_id"]
        assert transport.posts == 1 and len(env.calls) == 2
        await restored.refresh()
        assert len(restored.events[restored.snapshot["main"]["conversation_id"]]) == 2


async def test_unknown_request_hash_never_accepts_a_different_original(app_env, database, tmp_path):
    env = app_env
    device, _ = await owner(env, database)
    request = str(uuid4())
    response = await env.client.post(
        "/client/v1/messages",
        json={"request_id": request, "text": "remote original"},
        headers=bearer(device["token"]),
    )
    assert response.status_code == 200
    client = PersonalClient(
        "http://test", device["token"], Outbox(tmp_path / "outbox"), http=env.client
    )
    await client.prepare_message("different local body", request_id=request)
    client.outbox.update(request, "submitted")
    with pytest.raises(ClientProblem, match="request_conflict"):
        await client.submit(request)
    assert client.outbox.get(request)["state"] == "submitted"


async def test_outbox_cannot_be_rebound_to_another_person_after_rejection(
    app_env, database, tmp_path
):
    env = app_env
    first, _ = await owner(env, database)
    _, code = await person_and_code(env.client)
    second = await pair(env.client, code)
    box = Outbox(tmp_path / "owner.sqlite")
    a = PersonalClient("http://test", first["token"], box, http=env.client)
    request = await a.prepare_message("private unsent text")
    b = PersonalClient("http://test", second["token"], box, http=env.client)
    for _ in range(2):
        with pytest.raises(ValueError, match="outbox_owner_mismatch"):
            await b.submit(request)
    assert not env.calls and box.get(request)["state"] == "draft"


async def test_reference_upload_request_is_stable_and_resolves_registration(
    app_env, database, tmp_path
):
    env = app_env
    device, _ = await owner(env, database)
    client = PersonalClient(
        "http://test", device["token"], Outbox(tmp_path / "files.sqlite"), http=env.client
    )
    file = tmp_path / "document.txt"
    file.write_bytes(b"original file")
    request = await client.prepare_upload(file)
    uploaded = await client.submit(request)
    assert uploaded["state"] == "result" and uploaded["result"]["media_id"]
    assert (await client.resolve(request))["state"] == "result"
    changed = await client.prepare_upload(file)
    file.write_bytes(b"later replacement")
    with pytest.raises(ClientProblem, match="local_file_changed"):
        await client.submit(changed)
    assert client.outbox.get(changed)["state"] == "draft" and not env.calls


async def test_work_input_persists_original_target_and_server_fingerprint(
    app_env, database, tmp_path
):
    from tests.unit.test_client_works import seeded

    env = app_env
    device, _ = await owner(env, database)
    original = await seeded(env, database, group=True, state="waiting_user")
    client = PersonalClient(
        "http://test", device["token"], Outbox(tmp_path / "work.sqlite"), http=env.client
    )
    request = await client.prepare_input(original.row["id"], "answer from device")
    result = await client.submit(request)
    assert result["state"] == "accepted" and result["result"]["input_id"]
    resolved = await client.resolve(request)
    assert resolved["state"] == "accepted" and resolved["result"]["target"] == original.row["id"]
    assert not env.calls


async def test_known_terminal_failure_keeps_original_state_and_result(app_env, database, tmp_path):
    from tests.unit.test_client_works import seeded

    env = app_env
    device, _ = await owner(env, database)
    original = await seeded(env, database, state="completed")
    client = PersonalClient(
        "http://test", device["token"], Outbox(tmp_path / "terminal.sqlite"), http=env.client
    )
    request = await client.prepare_input(original.row["id"], "too late")
    with pytest.raises(ClientProblem) as first:
        await client.submit(request)
    assert first.value.code == "precondition_failed" and first.value.details["state"] == "completed"
    assert client.outbox.get(request)["state"] == "result"
    restored = PersonalClient(
        "http://test", device["token"], Outbox(client.outbox.path), http=env.client
    )
    with pytest.raises(ClientProblem) as replay:
        await restored.submit(request)
    assert replay.value.details["state"] == "completed" and not env.calls


async def test_upload_lost_ack_reads_same_registered_reference(app_env, database, tmp_path):
    env = app_env
    device, _ = await owner(env, database)
    source = tmp_path / "original.txt"
    source.write_bytes(b"persistent uploaded bytes")
    transport = DropAcknowledgement(env.app, resource="uploads")
    async with httpx.AsyncClient(transport=transport) as http:
        client = PersonalClient(
            "http://test", device["token"], Outbox(tmp_path / "upload.sqlite"), http=http
        )
        request = await client.prepare_upload(source)
        with pytest.raises(httpx.ReadTimeout):
            await client.submit(request)
        assert client.outbox.get(request)["state"] == "submitted"
        restored = PersonalClient(
            "http://test", device["token"], Outbox(client.outbox.path), http=http
        )
        result = await restored.submit(request)
        assert (
            result["state"] == "result"
            and result["result"]["media_id"]
            and result["result"]["filename"] == source.name
        )
        assert transport.posts == 1 and not env.calls


async def test_interrupted_upload_has_no_registration_and_same_id_can_retry(app_env, database):
    import hashlib

    from sqlalchemy import func, select

    from qq_ai_bot.conversation.canonical_db_models import ControlCommandReceiptModel

    env = app_env
    device, _ = await owner(env, database)
    principal = await env.identity.authenticate(device["token"])
    request = str(uuid4())
    content = b"complete file"

    async def interrupted():
        yield content[:4]
        raise ConnectionError("test disconnect")

    with pytest.raises(ConnectionError):
        await env.files.uploads.upload(
            principal,
            request,
            hashlib.sha256(content).hexdigest(),
            len(content),
            "file.txt",
            "file",
            interrupted(),
        )
    async with database.sessions() as session:
        assert (
            await session.scalar(
                select(func.count(ControlCommandReceiptModel.principal_id)).where(
                    ControlCommandReceiptModel.request_id == request
                )
            )
            == 0
        )

    async def complete():
        yield content

    result = await env.files.uploads.upload(
        principal,
        request,
        hashlib.sha256(content).hexdigest(),
        len(content),
        "file.txt",
        "file",
        complete(),
    )
    assert result["media_id"] and not list(env.files.uploads.root.glob("*.part"))
