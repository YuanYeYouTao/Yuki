"""Real personal routes keep original media, user selections and request receipts."""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import update
from tests.unit.test_admin_api import admin as admin_fixture
from tests.unit.test_app_messages import app_env as app_fixture
from tests.unit.test_app_messages import drain
from tests.unit.test_client_api import bearer, pair, person_and_code
from tests.unit.test_client_files import upload
from tests.unit.test_client_works import owner, seeded

from yuki.client_api.db_models import ClientDeviceModel
from yuki.external_agents.identity import ConnectorIdentityService
from yuki.external_agents.repository import AgentRepository, instance_key
from yuki.persistence.models import ChatEventModel, ConversationMediaItemModel

admin = admin_fixture
app_env = app_fixture


async def connector_target(env, database, device):
    principal = await env.identity.authenticate(device["token"])
    code = await env.identity.issue_for_device(principal, str(uuid4()), purpose="connector")
    connector = await ConnectorIdentityService(database).pair(code["code"], "test computer")
    await AgentRepository(database).register(
        connector["device_id"],
        [{"id": "test-codex", "label": "test Codex", "driver": "codex", "status": "available"}],
    )
    return connector, {
        "instance_id": instance_key(connector["device_id"], "test-codex"),
        "cwd": "/explicit/project",
        "intent": "prompt",
    }


async def test_message_target_reaches_main_and_original_receipt_survives_device_revocation(
    app_env, database
):
    env = app_env
    device, _ = await owner(env, database)
    connector, target = await connector_target(env, database, device)
    body = {"request_id": str(uuid4()), "text": "review my project", "agent_target": target}
    first = await env.client.post("/client/v1/messages", json=body, headers=bearer(device["token"]))
    assert first.status_code == 200, first.text
    await drain(env)
    assert any(
        target["instance_id"] in (message.content or "") for message in env.calls[0].messages
    )
    found = await env.client.get(
        f"/client/v1/requests/{body['request_id']}", headers=bearer(device["token"])
    )
    assert found.json()["data"]["found"]
    async with database.immediate_session() as writer:
        await writer.execute(
            update(ClientDeviceModel)
            .where(ClientDeviceModel.id == connector["device_id"])
            .values(revoked_at=datetime.now(UTC))
        )
    replay = await env.client.post(
        "/client/v1/messages", json=body, headers=bearer(device["token"])
    )
    assert replay.status_code == 200 and replay.json()["data"]["replay"]
    changed = {**body, "agent_target": {**target, "cwd": "/another/project"}}
    assert (
        await env.client.post("/client/v1/messages", json=changed, headers=bearer(device["token"]))
    ).status_code == 409
    fresh = {**body, "request_id": str(uuid4())}
    assert (
        await env.client.post("/client/v1/messages", json=fresh, headers=bearer(device["token"]))
    ).status_code == 401


async def test_work_target_is_original_input_and_changed_intent_conflicts(app_env, database):
    env = app_env
    device, _ = await owner(env, database)
    _, target = await connector_target(env, database, device)
    original = await seeded(env, database, state="waiting_user")
    work_id = original.row["id"]
    path = f"/client/v1/works/{work_id}/inputs"
    body = {"request_id": str(uuid4()), "text": "continue here", "agent_target": target}
    result = await env.client.post(path, json=body, headers=bearer(device["token"]))
    assert result.status_code == 200, result.text
    async with database.read_session() as reader:
        event = await reader.get(ChatEventModel, result.json()["data"]["event_id"])
        assert target["instance_id"] in event.content
        assert (
            next(
                part["data"]
                for part in json.loads(event.segments_json)
                if part["type"] == "agent_target"
            )
            == target
        )
    replay = await env.client.post(path, json=body, headers=bearer(device["token"]))
    assert replay.status_code == 200 and replay.json()["data"]["replay"]
    changed = {**body, "agent_target": {**target, "intent": "watch"}}
    assert (
        await env.client.post(path, json=changed, headers=bearer(device["token"]))
    ).status_code == 409
    found = await env.client.get(
        f"/client/v1/requests/{body['request_id']}", headers=bearer(device["token"])
    )
    assert found.status_code == 200 and found.json()["data"]["found"]
    current = await original.repository.get(work_id)
    assert current["model_requests"] == original.row["model_requests"]


async def attachment_request(env, database):
    device, _ = await owner(env, database)
    content = b"original private attachment"
    uploaded = (await upload(env, device, content)).json()["data"]
    result = await env.client.post(
        "/client/v1/messages",
        json={
            "request_id": str(uuid4()),
            "text": "original",
            "attachments": [uploaded["media_id"]],
        },
        headers=bearer(device["token"]),
    )
    assert result.status_code == 200, result.text
    await drain(env)
    result = result.json()["data"]
    path = (
        f"/client/v1/conversations/{result['conversation_id']}"
        f"/events/{result['event_id']}/attachments/0"
    )
    return device, content, result, path


async def test_original_attachment_http_and_history_indices(app_env, database):
    env = app_env
    device, content, result, path = await attachment_request(env, database)
    response = await env.client.get(path, headers=bearer(device["token"]))
    assert response.status_code == 200 and response.content == content, response.text
    assert response.headers["etag"] == f'"{hashlib.sha256(content).hexdigest()}"'
    events = await env.client.get(
        f"/client/v1/conversations/{result['conversation_id']}/events",
        headers=bearer(device["token"]),
    )
    item = next(row for row in events.json()["data"]["items"] if row["id"] == result["event_id"])
    assert item["attachments"][0]["attachment_index"] == 0
    assert item["attachments"][0]["segment_index"] == 1
    assert "author_account_id" in item and "ingress_presence_id" in item
    _, code = await person_and_code(env.client)
    stranger = await pair(env.client, code)
    assert (await env.client.get(path, headers=bearer(stranger["token"]))).status_code == 404
    assert (
        await env.client.get(
            path.replace(result["conversation_id"], str(uuid4())), headers=bearer(device["token"])
        )
    ).status_code == 404
    item, cached = await env.media.authorized_path(event_id=result["event_id"], attachment_index=0)
    cached.unlink()
    async with database.immediate_session() as writer:
        await writer.execute(
            update(ConversationMediaItemModel)
            .where(ConversationMediaItemModel.source_event_id == result["event_id"])
            .values(cache_status="expired", expires_at=datetime.now(UTC) - timedelta(days=1))
        )
    assert (await env.client.get(path, headers=bearer(device["token"]))).status_code == 404
    assert not cached.exists()


async def test_attachment_rechecks_identity_after_file_read(app_env, database, monkeypatch):
    env = app_env
    device, _, _, path = await attachment_request(env, database)
    original = env.media.authorized_path

    async def revoke_after_resolving(**kwargs):
        result = await original(**kwargs)
        async with database.immediate_session() as writer:
            await writer.execute(
                update(ClientDeviceModel)
                .where(
                    ClientDeviceModel.token_hash
                    == hashlib.sha256(device["token"].encode()).hexdigest()
                )
                .values(revoked_at=datetime.now(UTC))
            )
        return result

    monkeypatch.setattr(env.media, "authorized_path", revoke_after_resolving)
    assert (await env.client.get(path, headers=bearer(device["token"]))).status_code == 401
