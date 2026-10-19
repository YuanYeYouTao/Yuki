"""Two real personal devices read the same indexed original and transcript facts."""

import hashlib
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import delete
from tests.unit.test_admin_api import admin as admin_fixture
from tests.unit.test_app_messages import app_env as app_fixture
from tests.unit.test_app_messages import drain
from tests.unit.test_client_api import bearer, pair, person_and_code
from tests.unit.test_client_files import upload
from tests.unit.test_client_works import owner

from yuki.persistence.models import ChatEventModel, ConversationMediaItemModel

admin = admin_fixture
app_env = app_fixture


async def test_prefixed_validation_never_echoes_pairing_or_extra_secrets(app_env):
    outer = FastAPI()
    outer.mount("/role/yuki", app_env.app)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=outer), base_url="http://test"
    ) as client:
        response = await client.post(
            "/role/yuki/client/v1/pair",
            json={"code": "private-pair-code", "device_label": "", "extra": "private-extra-value"},
        )
    assert response.status_code == 422
    assert response.json() == {"problem": {"code": "validation_error"}}
    assert "private-" not in response.text


@pytest.mark.parametrize("kind", ["file", "image"])
async def test_second_device_reads_exact_attachment_and_foreign_device_cannot(
    app_env, database, kind
):
    env = app_env
    first, _ = await owner(env, database)
    principal = await env.identity.authenticate(first["token"])
    code = await env.identity.issue_for_device(principal, str(uuid4()))
    second = await pair(env.client, code["code"])
    content = b"original file bytes"
    media = (await upload(env, first, content, kind=kind)).json()["data"]
    sent = await env.client.post(
        "/client/v1/messages",
        headers=bearer(first["token"]),
        json={"request_id": str(uuid4()), "text": "original", "attachments": [media["media_id"]]},
    )
    await drain(env)
    event_id = sent.json()["data"]["event_id"]
    prefix = f"/client/v1/conversations/{principal.conversation_id}/events"
    response = await env.client.get(
        f"{prefix}/{event_id}/attachments/0", headers=bearer(second["token"])
    )
    assert response.status_code == 200, response.text
    assert response.content == content
    assert response.headers["x-yuki-sha256"] == hashlib.sha256(content).hexdigest()
    events = (await env.client.get(prefix, headers=bearer(second["token"]))).json()["data"]["items"]
    attachment = events[0]["attachments"][0]
    assert attachment["attachment_index"] == 0 and attachment["size"] == len(content)
    assert attachment["availability"] == "available"
    _, other = await person_and_code(env.client)
    stranger = await pair(env.client, other)
    assert (
        await env.client.get(
            f"{prefix}/{event_id}/attachments/0", headers=bearer(stranger["token"])
        )
    ).status_code == 404
    async with database.immediate_session() as session:
        await session.execute(delete(ChatEventModel).where(ChatEventModel.id == event_id))
    assert (
        await env.client.get(f"{prefix}/{event_id}/attachments/0", headers=bearer(second["token"]))
    ).status_code == 404


@pytest.mark.parametrize("reason", ["expired", "cleaned"])
async def test_expired_or_cleaned_original_is_not_downloaded_again(app_env, database, reason):
    env = app_env
    first, _ = await owner(env, database)
    principal = await env.identity.authenticate(first["token"])
    media = (await upload(env, first, b"old original")).json()["data"]
    sent = await env.client.post(
        "/client/v1/messages",
        headers=bearer(first["token"]),
        json={"request_id": str(uuid4()), "text": "original", "attachments": [media["media_id"]]},
    )
    await drain(env)
    event_id = sent.json()["data"]["event_id"]
    async with database.immediate_session() as session:
        item = await session.get(ConversationMediaItemModel, (event_id, 0))
        if reason == "expired":
            item.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        else:
            env.media._path(item).unlink()
    response = await env.client.get(
        f"/client/v1/conversations/{principal.conversation_id}/events/{event_id}/attachments/0",
        headers=bearer(first["token"]),
    )
    assert response.json()["problem"] == {"code": "media_unavailable", "reason": reason}


async def test_transcript_patch_changes_history_revision_without_a_new_event(app_env, database):
    env = app_env
    first, _ = await owner(env, database)
    principal = await env.identity.authenticate(first["token"])
    media = (await upload(env, first, b"voice bytes", name="voice.webm", kind="record")).json()[
        "data"
    ]
    sent = await env.client.post(
        "/client/v1/messages",
        headers=bearer(first["token"]),
        json={"request_id": str(uuid4()), "text": "voice", "attachments": [media["media_id"]]},
    )
    await drain(env)
    event_id = sent.json()["data"]["event_id"]
    url = f"/client/v1/conversations/{principal.conversation_id}/events"
    before = (await env.client.get(url, headers=bearer(first["token"]))).json()["data"]
    assert before["items"][0]["transcript"] == {"state": "none"}
    assert before["items"][0]["attachments"][0]["availability"] == "not_readable"
    async with database.immediate_session() as session:
        row = await session.get(ChatEventModel, event_id)
        from yuki.domain.audio import AudioTranscript, serialize_transcripts

        row.audio_transcript = serialize_transcripts(
            (
                AudioTranscript("current", 0, "later original transcript"),
                AudioTranscript("reply", 1, "quoted voice belongs to its original event"),
            )
        )
    after = (await env.client.get(url, headers=bearer(first["token"]))).json()["data"]
    assert after["latest_event_id"] == before["latest_event_id"]
    assert after["revision"] > before["revision"]
    assert after["items"][0]["transcript"] == {
        "state": "available",
        "text": "later original transcript",
    }
    assert (
        await env.client.get(f"{url}/{event_id}/attachments/0", headers=bearer(first["token"]))
    ).status_code == 404
