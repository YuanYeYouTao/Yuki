"""Real streaming HTTP, durable normalized files, CAS and original credentials."""

import asyncio
import hashlib
from dataclasses import replace
from io import BytesIO
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from PIL import Image, PngImagePlugin
from sqlalchemy import func, select
from tests.unit.test_admin_api import TOKEN
from tests.unit.test_admin_api import admin as admin_fixture
from tests.unit.test_client_api import bearer, pair, person_and_code

from yuki.admin_api.client import avatar_mutation
from yuki.admin_api.http import attach_admin_api
from yuki.character.avatar import AvatarStore, CharacterAvatars
from yuki.character.avatar_cli import normalized_png
from yuki.character.repository import CharacterRepository
from yuki.client_api.http import ClientApi, attach_client_api
from yuki.persistence.models import ChatEventModel
from yuki.runtime.work_schema_v1 import work

admin = admin_fixture


def png(color="blue", *, size=(1024, 1024), metadata=False):
    stream = BytesIO()
    info = PngImagePlugin.PngInfo()
    if metadata:
        info.add_text("source_note", "private original metadata")
    Image.new("RGB", size, color).save(stream, format="PNG", pnginfo=info)
    return stream.getvalue()


@pytest.fixture
async def avatar_env(admin, tmp_path):
    _, api, *_ = admin
    avatars = CharacterAvatars(api.identities, AvatarStore(tmp_path / "avatars"))
    app = FastAPI()
    attach_admin_api(app, lambda: replace(api, avatars=avatars))
    attach_client_api(app, lambda: ClientApi(api.identities, avatars=avatars))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        person, code = await person_and_code(client)
        first = await pair(client, code)
        await CharacterRepository(api.identities).set_owner(
            str(uuid4()), person, str(uuid4()), granted=True
        )
        issue = await api.identities.issue_for_device(
            await api.identities.authenticate(first["token"]), str(uuid4())
        )
        second = await pair(client, issue["code"])
        _, code = await person_and_code(client)
        other = await pair(client, code)
        yield SimpleNamespace(
            client=client,
            avatars=avatars,
            first=first,
            second=second,
            other=other,
            person=person,
            identity=api.identities,
        )


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
        headers={**bearer((device or env.first)["token"]), "Content-Type": "image/png"},
    )
    return response, params


async def profile(env):
    return (
        await env.client.get("/client/v1/character", headers=bearer(env.first["token"]))
    ).json()["data"]


async def test_set_replay_files_metadata_public_thumbnail_and_identity(avatar_env, database):
    env = avatar_env
    before = await profile(env)
    async with database.sessions() as session:
        events = await session.scalar(select(func.count()).select_from(ChatEventModel))
        works = await session.scalar(select(func.count()).select_from(work))
    image = png(metadata=True)
    changed, request = await upload(env, image, before["avatar_revision"])
    assert changed.status_code == 200, changed.text
    current = changed.json()["data"]
    assert current["id"] == before["id"] and current["revision"] == before["revision"]
    assert current["avatar_revision"] == before["avatar_revision"] + 1
    assert current["avatar"]["revision"] == current["avatar_revision"]
    digest = current["avatar"]["sha256"]
    for size in (1024, 512, 256, 128, 64):
        response = await env.client.get(
            f"/client/v1/character/avatar/{digest}/{size}.png", headers=bearer(env.first["token"])
        )
        assert response.status_code == 200 and "immutable" in response.headers["cache-control"]
        with Image.open(BytesIO(response.content)) as decoded:
            assert decoded.size == (size, size) and "source_note" not in decoded.info
            assert decoded.info["srgb"] == 0
    assert (await env.client.get(current["avatar"]["sizes"]["128"])).status_code == 200
    assert (await env.client.get(current["avatar"]["sizes"]["64"])).status_code == 401
    replay, _ = await upload(
        env, image, before["avatar_revision"], request_id=request["request_id"], device=env.second
    )
    assert replay.status_code == 200 and replay.json()["data"] == {**current, "replay": True}
    conflict, _ = await upload(
        env, png("red"), current["avatar_revision"], request_id=request["request_id"]
    )
    assert conflict.status_code == 409 and conflict.json()["problem"]["code"] == "request_conflict"
    async with database.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(ChatEventModel)) == events
        assert await session.scalar(select(func.count()).select_from(work)) == works
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
async def test_invalid_content_has_no_profile_or_receipt_effect(
    avatar_env, payload, overrides, problem
):
    env = avatar_env
    before = await profile(env)
    response, _ = await upload(env, payload, before["avatar_revision"], **overrides)
    assert response.status_code in (409, 422), response.text
    assert response.json()["problem"]["code"] == problem
    assert await profile(env) == before
    assert not list(env.avatars.store.root.glob(".avatar-*"))


async def test_two_writers_one_cas_and_no_asset_collection(avatar_env):
    env = avatar_env
    before = await profile(env)
    red, blue = png("red"), png("blue")
    results = await asyncio.gather(
        upload(env, red, before["avatar_revision"]),
        upload(env, blue, before["avatar_revision"], device=env.second),
    )
    assert sorted(response.status_code for response, _ in results) == [200, 409]
    current = await profile(env)
    assert current["avatar_revision"] == before["avatar_revision"] + 1
    for payload in (red, blue):
        sha256 = hashlib.sha256(payload).hexdigest()
        assert all(
            env.avatars.store.path(sha256, size).is_file() for size in (1024, 512, 256, 128, 64)
        )
    assert (await env.client.get(current["avatar"]["sizes"]["128"])).status_code == 200


async def test_upload_interruption_then_same_uuid_and_revoke_before_commit(avatar_env, monkeypatch):
    env = avatar_env
    content = png()
    request_id = str(uuid4())
    principal = await env.identity.authenticate(env.first["token"])

    async def broken():
        yield content[:100]
        raise RuntimeError("stream disconnected")

    with pytest.raises(RuntimeError, match="stream disconnected"):
        await env.avatars.change(
            request_id,
            0,
            action="set",
            sha256=hashlib.sha256(content).hexdigest(),
            size=len(content),
            chunks=broken(),
            principal=principal,
        )
    assert (await profile(env))["avatar"] is None
    assert not list(env.avatars.store.root.glob(".avatar-*"))
    done, _ = await upload(env, content, 0, request_id=request_id)
    assert done.status_code == 200, done.text
    store = env.avatars.store.store

    async def revoke(*args):
        result = await store(*args)
        await CharacterRepository(env.identity).set_owner(
            str(uuid4()), env.person, str(uuid4()), granted=False
        )
        return result

    monkeypatch.setattr(env.avatars.store, "store", revoke)
    denied, _ = await upload(env, png("red"), 1)
    assert denied.status_code == 403 and denied.json()["problem"]["code"] == "owner_required"
    assert (await profile(env))["avatar"]["sha256"] == hashlib.sha256(content).hexdigest()


async def test_defaults_null_revision_and_operator_uses_same_service(avatar_env):
    env = avatar_env
    original = await profile(env)
    reset = await env.client.request(
        "DELETE",
        "/client/v1/character/avatar",
        json={"request_id": str(uuid4()), "expected_revision": 0},
        headers=bearer(env.first["token"]),
    )
    assert reset.status_code == 200 and reset.json()["data"]["avatar"] is None
    assert reset.json()["data"]["avatar_revision"] == 1
    env.client.headers.update(bearer(TOKEN))
    default = await avatar_mutation(env.client, "set-default", png("red"))
    assert default["default_avatar"] is not None and default["avatar"] is None
    custom = await avatar_mutation(env.client, "set", png("green"))
    restored = await avatar_mutation(env.client, "reset", None)
    assert custom["avatar"]["sha256"] != restored["avatar"]["sha256"]
    assert restored["avatar"]["sha256"] == default["default_avatar"]["sha256"]
    assert restored["id"] == original["id"]
    denied = await env.client.post(
        "/admin/v1/character/avatar/default",
        params={
            "request_id": str(uuid4()),
            "expected_revision": restored["avatar_revision"],
            "sha256": hashlib.sha256(png()).hexdigest(),
            "size": len(png()),
        },
        content=png(),
        headers=bearer(env.first["token"]),
    )
    assert denied.status_code == 401


async def test_cli_normalization_orientation_and_metadata(tmp_path):
    path = tmp_path / "source.jpg"
    image = Image.new("RGB", (1600, 800), "red")
    exif = Image.Exif()
    exif[274] = 6
    exif[270] = "private source note"
    image.save(path, exif=exif)
    normalized = normalized_png(path)
    with Image.open(BytesIO(normalized)) as decoded:
        assert decoded.size == (1024, 1024) and decoded.format == "PNG"
        assert not decoded.getexif() and "exif" not in decoded.info


async def test_process_crash_after_publication_and_db_flush_recovers_same_request(
    avatar_env, database
):
    import json
    import sys

    env = avatar_env
    content = png("purple")
    payload = {
        "database": database.url,
        "root": str(env.avatars.store.root),
        "token": env.first["token"],
        "request_id": str(uuid4()),
        "png": content.hex(),
        "sha256": hashlib.sha256(content).hexdigest(),
    }
    child = r"""
import asyncio, json, os, sys
from pathlib import Path
from yuki.character.avatar import AvatarStore, CharacterAvatars
from yuki.client_api.identity import ClientIdentityService
from yuki.persistence.database import Database

async def main():
    payload = json.loads(sys.stdin.read())
    database = Database(payload["database"])
    identity = ClientIdentityService(database)
    principal = await identity.authenticate(payload["token"])
    service = CharacterAvatars(identity, AvatarStore(Path(payload["root"])))
    async def crash(session, **kwargs):
        await session.flush()
        os._exit(73)
    identity.receipts.record_personal_command = crash
    content = bytes.fromhex(payload["png"])
    async def chunks():
        yield content
    await service.change(payload["request_id"], 0, action="set", sha256=payload["sha256"],
                         size=len(content), chunks=chunks(), principal=principal)
asyncio.run(main())
"""
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        child,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _output, errors = await process.communicate(json.dumps(payload).encode())
    assert process.returncode == 73, errors.decode()
    unchanged = await profile(env)
    assert unchanged["avatar"] is None and unchanged["avatar_revision"] == 0
    assert all(
        env.avatars.store.path(payload["sha256"], size).is_file()
        for size in (1024, 512, 256, 128, 64)
    )
    retried, _ = await upload(env, content, 0, request_id=payload["request_id"])
    assert retried.status_code == 200, retried.text
    assert retried.json()["data"]["avatar_revision"] == 1
    replay, _ = await upload(env, content, 0, request_id=payload["request_id"], device=env.second)
    assert replay.status_code == 200 and replay.json()["data"]["replay"]
    assert (await profile(env))["avatar_revision"] == 1


async def test_concurrent_same_content_same_uuid_keeps_one_receipt(avatar_env, database):
    from yuki.conversation.canonical_db_models import ControlCommandReceiptModel

    env = avatar_env
    request_id = str(uuid4())
    results = await asyncio.gather(
        upload(env, png(), 0, request_id=request_id),
        upload(env, png(), 0, request_id=request_id, device=env.second),
    )
    assert [response.status_code for response, _ in results] == [200, 200]
    assert (await profile(env))["avatar_revision"] == 1
    async with database.sessions() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(ControlCommandReceiptModel)
                .where(
                    ControlCommandReceiptModel.request_id == request_id,
                    ControlCommandReceiptModel.operation_kind == "client.character_avatar",
                )
            )
            == 1
        )


async def test_actual_cli_parse_normalize_set_default_and_reset(
    avatar_env, tmp_path, monkeypatch, capsys
):
    import argparse
    import json
    from contextlib import asynccontextmanager

    from yuki.admin_api.cli import add_admin_parser, admin_command

    env = avatar_env
    env.client.headers.update(bearer(TOKEN))

    @asynccontextmanager
    async def connected(_settings):
        yield env.client

    monkeypatch.setattr("yuki.admin_api.cli.admin_client", connected)
    parser = argparse.ArgumentParser()
    add_admin_parser(parser.add_subparsers(dest="command", required=True))
    source = tmp_path / "source.png"
    source.write_bytes(png("orange", size=(64, 96), metadata=True))
    states = []
    for action in ("set-default", "set", "reset"):
        arguments = ["admin", "character", "avatar", action]
        if action != "reset":
            arguments.append(str(source))
        args = parser.parse_args(arguments)
        assert await admin_command(None, args) == 0
        states.append(json.loads(capsys.readouterr().out))
    assert states[0]["avatar"] is None
    assert states[1]["avatar"]["sha256"] == states[0]["default_avatar"]["sha256"]
    assert states[2]["avatar"]["sha256"] == states[0]["default_avatar"]["sha256"]
    assert [state["avatar_revision"] for state in states] == [1, 2, 3]


async def test_output_disk_failure_is_unavailable_without_profile_effect(avatar_env, monkeypatch):
    import errno

    env = avatar_env
    original = Image.Image.save

    def full(self, path, *args, **kwargs):
        if not isinstance(path, BytesIO):
            raise OSError(errno.ENOSPC, "disk full")
        return original(self, path, *args, **kwargs)

    monkeypatch.setattr(Image.Image, "save", full)
    result, _ = await upload(env, png(), 0)
    assert result.status_code == 503 and result.json()["problem"]["code"] == "unavailable"
    assert (await profile(env))["avatar_revision"] == 0
    assert not list(env.avatars.store.root.glob(".avatar-*"))


def test_avatar_binary_schema_and_null_revision_are_explicit():
    from yuki.client_api.openapi import client_openapi

    schema = client_openapi()
    upload = schema["paths"]["/client/v1/character/avatar"]["put"]
    assert upload["requestBody"]["content"]["image/png"]["schema"] == {
        "type": "string",
        "format": "binary",
    }
    download = schema["paths"]["/client/v1/character/avatar/{sha256}/{size}.png"]["get"]
    assert download["responses"]["200"]["content"]["image/png"]["schema"]["format"] == "binary"
    assert "avatar_revision" in schema["components"]["schemas"]["CharacterProfile"]["required"]
