"""Uploads, normal media preparation, and receipt-authorized original files."""

import hashlib
import io
import json
from uuid import uuid4

import pytest
from PIL import Image
from sqlalchemy import delete, func, select
from tests.support.workspace_snapshots import snapshot_bytes
from tests.unit.test_admin_api import admin as admin_fixture
from tests.unit.test_app_messages import app_env as app_fixture
from tests.unit.test_app_messages import drain
from tests.unit.test_client_api import bearer, pair, person_and_code
from tests.unit.test_client_works import owner, seeded

from qq_ai_bot.conversation.canonical_db_models import ControlCommandReceiptModel
from qq_ai_bot.persistence.models import ChatEventModel, ConversationMediaItemModel
from qq_ai_bot.services.media_resolver import MediaResolutionError, read_local_media
from qq_ai_bot.social.service import SocialContext

admin = admin_fixture
app_env = app_fixture


async def upload(
    env, device, content, *, request_id=None, name="input.txt", kind="file", sha256=None
):
    return await env.client.post(
        "/client/v1/uploads",
        params={
            "request_id": request_id or str(uuid4()),
            "sha256": sha256 or hashlib.sha256(content).hexdigest(),
            "size": len(content),
            "filename": name,
            "kind": kind,
        },
        content=content,
        headers=bearer(device["token"]),
    )


async def test_upload_registers_receipt_without_sending_and_replays_across_devices(
    app_env, database
):
    env = app_env
    device, _ = await owner(env, database)
    code = await env.identity.issue_for_device(
        await env.identity.authenticate(device["token"]), str(uuid4())
    )
    second = await pair(env.client, code["code"])
    request_id = str(uuid4())
    first = await upload(env, device, b"test bytes", request_id=request_id)
    replay = await upload(env, second, b"test bytes", request_id=request_id)
    assert first.status_code == replay.status_code == 200, first.text
    assert replay.json()["data"] == {**first.json()["data"], "replay": True}
    assert not env.calls
    async with database.sessions() as session:
        assert await session.scalar(select(func.count(ChatEventModel.id))) == 0
        assert (
            await session.scalar(
                select(func.count(ControlCommandReceiptModel.principal_id)).where(
                    ControlCommandReceiptModel.operation_kind == "client.upload"
                )
            )
            == 1
        )
    found = await env.client.get(
        f"/client/v1/requests/{request_id}", headers=bearer(second["token"])
    )
    assert (
        found.json()["data"]["operation_kind"] == "client.upload"
        and found.json()["data"]["result"]["media_id"] == first.json()["data"]["media_id"]
    )
    conflict = await upload(env, device, b"changed", request_id=request_id)
    assert conflict.status_code == 409


async def test_bad_digest_rolls_back_and_published_orphan_is_reference_checked(
    app_env, database, monkeypatch
):
    env = app_env
    device, _ = await owner(env, database)
    bad = await upload(env, device, b"corrupt", sha256="a" * 64)
    assert bad.status_code == 422
    assert not list(env.files.uploads.root.glob("*"))
    original = env.identity.receipts.record_personal_command

    async def fail(session, **kwargs):
        assert env.files.uploads.path(hashlib.sha256(b"orphan").hexdigest()).exists()
        await original(session, **kwargs)
        raise RuntimeError("failed registration")

    monkeypatch.setattr(env.identity.receipts, "record_personal_command", fail)
    principal = await env.identity.authenticate(device["token"])

    async def chunks():
        yield b"or"
        yield b"phan"

    with pytest.raises(RuntimeError, match="failed registration"):
        await env.files.uploads.upload(
            principal,
            str(uuid4()),
            hashlib.sha256(b"orphan").hexdigest(),
            6,
            "input.txt",
            "file",
            chunks(),
        )
    async with database.sessions() as session:
        assert (
            await session.scalar(
                select(func.count(ControlCommandReceiptModel.principal_id)).where(
                    ControlCommandReceiptModel.operation_kind == "client.upload"
                )
            )
            == 0
        )
    orphan = env.files.uploads.path(hashlib.sha256(b"orphan").hexdigest())
    assert orphan.exists()
    await env.media.cleanup()
    assert not orphan.exists()


@pytest.mark.parametrize("kind", ["file", "image"])
async def test_personal_attachment_enters_original_main_and_media_index(app_env, database, kind):
    env = app_env
    device, _ = await owner(env, database)
    if kind == "image":
        output = io.BytesIO()
        Image.new("RGB", (24, 24), "blue").save(output, format="PNG")
        content, name = output.getvalue(), "photo.png"
    else:
        content, name = b"THE ORIGINAL DOCUMENT CONTENT", "document.txt"
    uploaded = await upload(env, device, content, name=name, kind=kind)
    assert uploaded.status_code == 200, uploaded.text
    media = uploaded.json()["data"]
    body = {"request_id": str(uuid4()), "text": "read this", "attachments": [media["media_id"]]}
    sent = await env.client.post("/client/v1/messages", json=body, headers=bearer(device["token"]))
    assert sent.status_code == 200, sent.text
    await drain(env)
    assert env.calls
    messages = env.calls[0].messages
    if kind == "image":
        assert any(item.images for item in messages)
    else:
        assert any("THE ORIGINAL DOCUMENT CONTENT" in (item.content or "") for item in messages)
    async with database.sessions() as session:
        row = await session.get(ConversationMediaItemModel, (sent.json()["data"]["event_id"], 0))
        assert row.cache_status == "cached" and row.content_sha256 == media["sha256"]
        event = await session.get(ChatEventModel, row.source_event_id)
        assert (
            "base64" not in event.segments_json
            and str(env.files.uploads.root) not in event.segments_json
        )
    await env.media.cleanup()
    assert env.files.uploads.path(media["sha256"]).exists()
    replay = await env.client.post(
        "/client/v1/messages", json=body, headers=bearer(device["token"])
    )
    assert (
        replay.json()["data"]["event_id"] == sent.json()["data"]["event_id"]
        and replay.json()["data"]["replay"]
    )
    assert len(env.calls) == 2


async def test_foreign_upload_is_not_usable_and_changed_blob_fails_integrity(app_env, database):
    env = app_env
    device, _ = await owner(env, database)
    _, code = await person_and_code(env.client)
    stranger = await pair(env.client, code)
    uploaded = (await upload(env, device, b"private bytes")).json()["data"]
    denied = await env.client.post(
        "/client/v1/messages",
        json={"request_id": str(uuid4()), "text": "steal", "attachments": [uploaded["media_id"]]},
        headers=bearer(stranger["token"]),
    )
    assert denied.status_code == 404 and not env.calls
    path = env.files.uploads.path(uploaded["sha256"])
    path.write_bytes(b"changed bytes")
    with pytest.raises(MediaResolutionError, match="附件内容已变化"):
        read_local_media((path, uploaded["sha256"]), 1024)


async def app_context(env, device):
    request = {"request_id": str(uuid4()), "text": "prepare result"}
    response = await env.client.post(
        "/client/v1/messages", json=request, headers=bearer(device["token"])
    )
    await drain(env)
    principal = await env.identity.authenticate(device["token"])
    return SocialContext(
        f"app-file:{uuid4()}",
        "file",
        principal.conversation_id,
        person_refs={"current_speaker": principal.person_id},
        trigger_event_id=response.json()["data"]["event_id"],
        reply_presence_id=env.gateway.registry.resolve_by_handle(
            env.gateway.handle
        ).snapshot.presence_id,
    )


async def test_local_artifact_pins_original_snapshot_and_download_authorization(app_env, database):
    env = app_env
    device, _ = await owner(env, database)
    context = await app_context(env, device)
    original = snapshot_bytes(env.store, "result.txt", b"original version")
    result = await env.social.execute(
        "send_message",
        {"artifact_id": original["artifact_id"], "attachment_kind": "file", "text": "result"},
        context,
    )
    assert result["status"] == "succeeded" and result["platform_reference"] is None
    snapshot_bytes(env.store, "result.txt", b"replacement version")
    url = f"/client/v1/artifacts/{original['artifact_id']}?version={original['sha256']}"
    downloaded = await env.client.get(url, headers=bearer(device["token"]))
    assert downloaded.status_code == 200 and downloaded.content == b"original version"
    assert downloaded.headers["etag"] == f'"{original["sha256"]}"'
    _, code = await person_and_code(env.client)
    stranger = await pair(env.client, code)
    assert (await env.client.get(url, headers=bearer(stranger["token"]))).status_code == 404
    assert (
        await env.client.get(
            url.replace(original["sha256"], "a" * 64), headers=bearer(device["token"])
        )
    ).status_code == 404
    async with database.immediate_session() as session:
        await session.execute(delete(ChatEventModel).where(ChatEventModel.id == result["event_id"]))
    assert (await env.client.get(url, headers=bearer(device["token"]))).status_code == 404


async def test_local_audio_uses_original_asr_and_does_not_store_bytes(
    app_env, database, monkeypatch
):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from tests.conftest import make_settings

    from qq_ai_bot.asr.service import ASRService

    env = app_env
    device, _ = await owner(env, database)
    monkeypatch.setattr(
        "qq_ai_bot.asr.service.prepare_audio", AsyncMock(return_value=b"normalized")
    )
    provider = SimpleNamespace(transcribe=AsyncMock(return_value="recognized original voice"))
    service = ASRService(
        settings=make_settings(database.url, asr_enabled=True).asr,
        provider=provider,
        resolver=env.media.resolver,
    )
    principal = await env.identity.authenticate(device["token"])
    uploaded = (
        await upload(env, device, b"local audio bytes", name="voice.mp3", kind="record")
    ).json()["data"]
    segments, attachments = await env.files.attachments(principal, (uploaded["media_id"],))
    from qq_ai_bot.identity.db_models import IdentityBindingModel

    async with database.sessions() as session:
        binding = await session.get(IdentityBindingModel, principal.binding_id)
    message = env.gateway.message(binding, str(uuid4()), "voice", segments, attachments)
    result = await service.prepare(message, None)
    assert provider.transcribe.await_args.args == (b"normalized",)
    assert "recognized original voice" in result.transcript and not result.error
    assert "local audio bytes" not in result.transcript


async def test_group_result_file_is_readable_only_through_owned_work(app_env, database):
    from datetime import UTC, datetime

    from sqlalchemy import insert

    from qq_ai_bot.domain.conversations import ConversationScope
    from qq_ai_bot.runtime.work_schema_v1 import effects
    from qq_ai_bot.social.db_models import SocialOperationModel

    env = app_env
    device, _ = await owner(env, database)
    original = await seeded(env, database, group=True)
    await seeded(env, database, account="1002", group=True)
    second, _ = await owner(env, database, "1002")
    snapshot = snapshot_bytes(env.store, "group-result.txt", b"only this work result")
    event = (
        await env.harness.scoped_events.append(
            scope=ConversationScope.group("8000", "2001"),
            platform_message_id=str(uuid4()),
            sender_user_id="8000",
            direction="outbound",
            content="file",
            segments=(
                {
                    "type": "file",
                    "data": {"artifact_id": snapshot["artifact_id"], "version": snapshot["sha256"]},
                },
            ),
        )
    ).event
    operation = str(uuid4())
    async with database.immediate_session() as session:
        session.add(
            SocialOperationModel(
                id=operation,
                source_turn_id=f"{event.canonical_conversation_id}:event:{original.event.id}",
                tool_call_id=str(uuid4()),
                source_conversation_id=event.canonical_conversation_id,
                action="send_message",
                payload_hash="a" * 64,
                target_kind="space",
                target_id=event.canonical_conversation_id,
                presence_id=original.event.ingress_presence_id,
                status="succeeded",
                event_id=event.id,
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
        )
        await session.execute(
            insert(effects).values(
                effect_key=str(uuid4()),
                work_id=original.row["id"],
                kind="social",
                state="accepted",
                receipt_json=json.dumps(
                    {"invocation": {"original_domain_ref": f"social:{operation}"}}
                ),
                created=1.0,
                updated=1.0,
            )
        )
    path = f"/client/v1/artifacts/{snapshot['artifact_id']}?version={snapshot['sha256']}"
    own = await env.client.get(path, headers=bearer(device["token"]))
    assert own.status_code == 200 and own.content == b"only this work result"
    assert (await env.client.get(path, headers=bearer(second["token"]))).status_code == 404
