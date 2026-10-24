"""Real avatar HTTP, immutable files, original receipts and current owner checks."""

import asyncio
import hashlib
import json
from io import BytesIO
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from PIL import Image, PngImagePlugin
from sqlalchemy import func, select
from tests.conftest import make_settings
from tests.unit.test_client_identity import code_for

from yuki.character.avatar import AvatarStore, CharacterAvatars
from yuki.character.db_models import CharacterProfileModel
from yuki.character.repository import CharacterRepository
from yuki.client_api.http import ClientApi, attach_client_api
from yuki.client_api.identity import fingerprint
from yuki.client_api.openapi import client_openapi
from yuki.client_api.views import ClientViews
from yuki.client_api.works import ClientWorks
from yuki.conversation.canonical_db_models import ControlCommandReceiptModel


def png(color="blue", *, size=(1024, 1024), metadata=False):
    stream = BytesIO()
    info = PngImagePlugin.PngInfo()
    if metadata:
        info.add_text("source_note", "private original metadata")
    with Image.new("RGB", size, color) as image:
        image.save(stream, format="PNG", pnginfo=info)
    return stream.getvalue()


@pytest.fixture
async def avatar_env(database, tmp_path):
    identity, person, issued = await code_for(database)
    first = await identity.pair(issued["code"], "owner")
    principal = await identity.authenticate(first["token"])
    following = await identity.issue_for_device(principal, str(uuid4()))
    second = await identity.pair(following["code"], "same person")
    _, _person, other_code = await code_for(database, "1002")
    other = await identity.pair(other_code["code"], "viewer")
    repository = CharacterRepository(identity, make_settings(database.url))
    await repository.initialize("Avatar character")
    await repository.set_owner(str(uuid4()), person, str(uuid4()), granted=True)
    avatars = CharacterAvatars(repository, AvatarStore(tmp_path / "avatars"))
    views = ClientViews(identity, ClientWorks(identity, {}), character=repository)
    app = FastAPI()
    attach_client_api(
        app, lambda: ClientApi(identity, character=repository, avatars=avatars, views=views)
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield SimpleNamespace(
            client=client,
            avatars=avatars,
            repository=repository,
            identity=identity,
            views=views,
            first=first,
            second=second,
            other=other,
            principal=principal,
            person=person,
        )


def bearer(device):
    return {"Authorization": "Bearer " + device["token"]}


async def profile(env):
    response = await env.client.get("/client/v1/character", headers=bearer(env.first))
    assert response.status_code == 200, response.text
    return response.json()["data"]


async def upload(env, content, revision, *, request_id=None, device=None, **overrides):
    params = {
        "request_id": request_id or str(uuid4()),
        "expected_revision": revision,
        "sha256": hashlib.sha256(content).hexdigest(),
        "size": len(content),
        **overrides,
    }
    response = await env.client.put(
        "/client/v1/character/avatar",
        params=params,
        content=content,
        headers={**bearer(device or env.first), "Content-Type": "image/png"},
    )
    return response, params


async def test_set_replay_immutable_variants_and_original_request(avatar_env, database):
    env = avatar_env
    before = await profile(env)
    me = (await env.client.get("/client/v1/me", headers=bearer(env.first))).json()["data"]
    changed, request = await upload(env, png(metadata=True), 0)
    assert changed.status_code == 200, changed.text
    current = changed.json()["data"]
    assert current["id"] == before["id"] and current["revision"] == before["revision"]
    assert current["avatar_revision"] == current["avatar"]["revision"] == 1
    digest = current["avatar"]["sha256"]
    for size in (1024, 512, 256, 128, 64):
        response = await env.client.get(
            f"/client/v1/character/avatar/{digest}/{size}.png", headers=bearer(env.first)
        )
        assert response.status_code == 200 and "immutable" in response.headers["cache-control"]
        with Image.open(BytesIO(response.content)) as decoded:
            assert decoded.size == (size, size) and "source_note" not in decoded.info
            assert decoded.info["srgb"] == 0
    assert (await env.client.get(current["avatar"]["sizes"]["128"])).status_code == 200
    assert (await env.client.get(current["avatar"]["sizes"]["64"])).status_code == 401
    assert (
        await env.client.get(current["avatar"]["sizes"]["64"], headers=bearer(env.other))
    ).status_code == 200
    meta = (await env.client.get("/client/v1/meta")).json()["data"]
    assert meta["api_revision"] == 6 and "character_avatar" in meta["capabilities"]
    assert meta["character"]["avatar_url"] == current["avatar"]["sizes"]["128"]
    replay, _ = await upload(
        env, png(metadata=True), 0, request_id=request["request_id"], device=env.second
    )
    assert replay.status_code == 200 and replay.json()["data"] == {**current, "replay": True}
    original = (
        await env.client.get(
            "/client/v1/requests/" + request["request_id"], headers=bearer(env.second)
        )
    ).json()["data"]
    assert original["found"] and original["operation_kind"] == "client.character_avatar"
    assert original["target"] == before["id"] and original["status"] == "succeeded"
    assert original["payload_hash"] == fingerprint(
        "client.character_avatar",
        before["id"],
        {"action": "set", **{key: value for key, value in request.items() if key != "request_id"}},
    )
    assert original["result"]["avatar"]["sha256"] == digest
    conflict, _ = await upload(env, png("red"), 1, request_id=request["request_id"])
    assert conflict.status_code == 409 and conflict.json()["problem"]["code"] == "request_conflict"
    after = (await env.client.get("/client/v1/me", headers=bearer(env.first))).json()["data"]
    for key in (
        "person_id",
        "binding_id",
        "conversation_id",
        "source_person_id",
        "request_person_id",
        "request_conversation_id",
        "request_scope",
    ):
        assert after[key] == me[key]
    assert {item["device_id"] for item in after["devices"]} == {
        item["device_id"] for item in me["devices"]
    }
    async with database.read_session() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(ControlCommandReceiptModel)
                .where(ControlCommandReceiptModel.request_id == request["request_id"])
            )
            == 1
        )
    assert not list(env.avatars.store.root.glob(".avatar-*"))


@pytest.mark.parametrize(
    "payload,overrides,problem",
    [
        (b"not a PNG", {}, "unsupported_image"),
        (png(size=(1000, 1024)), {}, "image_dimensions"),
        (png()[:100], {}, "unsupported_image"),
        (png(), {"sha256": "0" * 64}, "validation_error"),
        (png(), {"size": 1}, "validation_error"),
        (png(), {"size": len(png()) + 1}, "validation_error"),
    ],
)
async def test_invalid_image_keeps_profile_and_original_request_unwritten(
    avatar_env, payload, overrides, problem
):
    before = await profile(avatar_env)
    response, request = await upload(avatar_env, payload, 0, **overrides)
    assert response.status_code in (409, 422), response.text
    assert response.json()["problem"]["code"] == problem
    assert await profile(avatar_env) == before
    receipt = await avatar_env.client.get(
        "/client/v1/requests/" + request["request_id"], headers=bearer(avatar_env.first)
    )
    assert receipt.json()["data"]["found"] is False
    assert not list(avatar_env.avatars.store.root.glob(".avatar-*"))


async def test_two_devices_one_cas_and_old_assets_remain_readable(avatar_env):
    env = avatar_env
    contents = (png("red"), png("blue"))
    results = await asyncio.gather(
        upload(env, contents[0], 0), upload(env, contents[1], 0, device=env.second)
    )
    assert sorted(response.status_code for response, _ in results) == [200, 409]
    assert (await profile(env))["avatar_revision"] == 1
    for content in contents:
        digest = hashlib.sha256(content).hexdigest()
        for size in (1024, 512, 256, 128, 64):
            assert env.avatars.store.path(digest, size).is_file()
        image = await env.client.get(
            f"/client/v1/character/avatar/{digest}/64.png", headers=bearer(env.first)
        )
        assert image.status_code == 200


async def test_reset_to_letter_keeps_revision_and_original_receipt(avatar_env):
    env = avatar_env
    saved, original = await upload(env, png(), 0)
    assert saved.status_code == 200
    body = {"request_id": str(uuid4()), "expected_revision": 1}
    reset = await env.client.request(
        "DELETE", "/client/v1/character/avatar", json=body, headers=bearer(env.first)
    )
    assert reset.status_code == 200, reset.text
    result = reset.json()["data"]
    assert result["avatar"] is None and result["default_avatar"] is None
    assert result["avatar_revision"] == 2
    replay = await env.client.request(
        "DELETE", "/client/v1/character/avatar", json=body, headers=bearer(env.second)
    )
    assert replay.status_code == 200 and replay.json()["data"]["replay"] is True
    receipt = (
        await env.client.get("/client/v1/requests/" + body["request_id"], headers=bearer(env.first))
    ).json()["data"]
    assert receipt["target"] == result["id"]
    assert receipt["payload_hash"] == fingerprint(
        "client.character_avatar",
        result["id"],
        {"action": "reset", "expected_revision": 1, "sha256": None, "size": None},
    )
    old_set, _ = await upload(env, png(), 0, request_id=original["request_id"])
    assert old_set.status_code == 200 and old_set.json()["data"]["avatar"]["sha256"]
    current = await profile(env)
    assert current["avatar"] is None and current["avatar_revision"] == 2
    stale, _ = await upload(env, png("red"), 1)
    assert stale.status_code == 409 and stale.json()["problem"]["code"] == "version_conflict"
    assert stale.json()["problem"]["current"]["avatar_revision"] == 2


async def test_reset_uses_existing_default_without_reinitializing_character(avatar_env, database):
    env = avatar_env
    content = png("purple")
    digest = hashlib.sha256(content).hexdigest()

    async def chunks():
        yield content

    await env.avatars.store.store(chunks(), digest, len(content))
    async with database.immediate_session() as session:
        row = await session.get(CharacterProfileModel, (await env.repository.current(session)).id)
        row.default_avatar_sha256 = digest
    current, _ = await upload(env, png(), 0)
    assert (
        current.status_code == 200 and current.json()["data"]["default_avatar"]["sha256"] == digest
    )
    reset = await env.client.request(
        "DELETE",
        "/client/v1/character/avatar",
        json={"request_id": str(uuid4()), "expected_revision": 1},
        headers=bearer(env.first),
    )
    assert reset.status_code == 200, reset.text
    assert reset.json()["data"]["avatar"]["sha256"] == digest


async def test_owner_is_checked_before_consuming_image_and_after_publication(
    avatar_env, monkeypatch
):
    env = avatar_env
    consumed = False

    async def input_image():
        nonlocal consumed
        consumed = True
        yield png()

    denied = await env.client.put(
        "/client/v1/character/avatar",
        params={"request_id": str(uuid4()), "expected_revision": 0, "sha256": "0" * 64, "size": 1},
        headers=bearer(env.other),
        content=input_image(),
    )
    assert denied.status_code == 403 and consumed is False
    store = env.avatars.store.store

    async def revoke_owner(*args):
        result = await store(*args)
        await env.repository.set_owner(str(uuid4()), env.person, str(uuid4()), granted=False)
        return result

    monkeypatch.setattr(env.avatars.store, "store", revoke_owner)
    response, request = await upload(env, png(), 0)
    assert response.status_code == 403 and response.json()["problem"]["code"] == "owner_required"
    assert (await profile(env))["avatar"] is None
    receipt = await env.client.get(
        "/client/v1/requests/" + request["request_id"], headers=bearer(env.first)
    )
    assert receipt.json()["data"]["found"] is False


async def test_device_revoked_after_actual_file_read_gets_no_image(avatar_env, monkeypatch):
    from yuki.character import avatar as module

    env = avatar_env
    response, _ = await upload(env, png(), 0)
    digest = response.json()["data"]["avatar"]["sha256"]
    read_io = module._media_io

    async def revoke_after_read(function, *args):
        result = await read_io(function, *args)
        if getattr(function, "__name__", "") == "read_bytes":
            second = await env.identity.authenticate(env.second["token"])
            await env.identity.revoke(second, env.principal.device_id, str(uuid4()))
        return result

    monkeypatch.setattr(module, "_media_io", revoke_after_read)
    image = await env.client.get(
        f"/client/v1/character/avatar/{digest}/64.png", headers=bearer(env.first)
    )
    assert image.status_code == 401 and image.json()["problem"]["code"] == "unauthenticated"


async def test_public_thumbnail_changed_during_file_read_is_not_returned(avatar_env, monkeypatch):
    from yuki.character import avatar as module

    env = avatar_env
    first, _ = await upload(env, png(), 0)
    old_path = first.json()["data"]["avatar"]["sizes"]["128"]
    read_io = module._media_io

    async def change_after_read(function, *args):
        result = await read_io(function, *args)
        if getattr(function, "__name__", "") == "read_bytes":
            changed, _ = await upload(env, png("red"), 1)
            assert changed.status_code == 200
        return result

    monkeypatch.setattr(module, "_media_io", change_after_read)
    old = await env.client.get(old_path)
    assert old.status_code == 401 and old.json()["problem"]["code"] == "unauthenticated"


async def test_avatar_commit_publishes_existing_character_hint(avatar_env):
    env = avatar_env
    stream = env.views.stream(env.principal)

    def decode(frame):
        fields = dict(line.split(": ", 1) for line in frame.strip().splitlines())
        return fields["event"], json.loads(fields["data"])

    kind, initial = decode(await anext(stream))
    assert kind == "sync" and initial["character"]["avatar_revision"] == 0
    pending = asyncio.create_task(anext(stream))
    try:
        saved, _ = await upload(env, png(), 0)
        assert saved.status_code == 200
        kind, changed = decode(await asyncio.wait_for(pending, 2))
        assert kind == "character" and changed["avatar_revision"] == 1
        assert changed["revision"] == initial["character"]["revision"]
    finally:
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        await stream.aclose()


async def test_interrupted_upload_keeps_sql_writer_available_and_no_receipt(avatar_env, database):
    from sqlalchemy import text

    env = avatar_env
    started, release = asyncio.Event(), asyncio.Event()
    content = png()
    request_id = str(uuid4())

    async def interrupted():
        yield content[:100]
        started.set()
        await release.wait()
        raise RuntimeError("disconnected original upload")

    pending = asyncio.create_task(
        env.avatars.change(
            request_id,
            0,
            action="set",
            sha256=hashlib.sha256(content).hexdigest(),
            size=len(content),
            chunks=interrupted(),
            principal=env.principal,
        )
    )

    async def unrelated_writer():
        async with database.immediate_session() as session:
            await session.execute(text("SELECT 1"))

    try:
        await asyncio.wait_for(started.wait(), 2)
        await asyncio.wait_for(unrelated_writer(), 2)
    finally:
        release.set()
    with pytest.raises(RuntimeError, match="disconnected original upload"):
        await pending
    assert (await profile(env))["avatar"] is None
    receipt = await env.client.get(
        "/client/v1/requests/" + request_id,
        headers=bearer(env.first),
    )
    assert receipt.json()["data"]["found"] is False
    assert not list(env.avatars.store.root.glob(".avatar-*"))


async def test_publication_failure_can_resume_same_original_request(avatar_env, monkeypatch):
    env = avatar_env
    publish = env.avatars.store._publish

    def published_then_failed(*args):
        publish(*args)
        raise OSError("lost file publication acknowledgement")

    monkeypatch.setattr(env.avatars.store, "_publish", published_then_failed)
    original_id = str(uuid4())
    failed, _ = await upload(env, png(), 0, request_id=original_id)
    assert failed.status_code == 503
    assert (await profile(env))["avatar"] is None
    monkeypatch.setattr(env.avatars.store, "_publish", publish)
    recovered, _ = await upload(env, png(), 0, request_id=original_id)
    assert recovered.status_code == 200 and recovered.json()["data"]["avatar_revision"] == 1


def test_actual_avatar_routes_export_binary_contract_and_original_ids():
    document = client_openapi()
    upload = document["paths"]["/client/v1/character/avatar"]["put"]
    assert upload["requestBody"]["content"]["image/png"]["schema"]["format"] == "binary"
    assert {parameter["name"] for parameter in upload["parameters"]} >= {
        "request_id",
        "expected_revision",
        "sha256",
        "size",
    }
    assert "/client/v1/character/avatar/{sha256}/{size}.png" in document["paths"]
    assert document["info"]["x-yuki-api-revision"] == 6
