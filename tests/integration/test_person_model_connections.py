"""Person-device model management through the real isolated Bot and fake upstream."""

import asyncio
import json
from pathlib import Path
from uuid import uuid4

from tests.integration.test_person_authority_http import authority_http as authority_http
from tests.integration.test_person_authority_http import pair


async def test_superuser_device_discovers_tests_and_saves_original_bundle(authority_http):
    client, state, root = authority_http
    admin, device_id = await pair(client, state["owner_pairing_code"])
    visitor, _ = await pair(client, state["nonowner_pairing_code"])
    before = (await client.get("admin/v1/models", headers=admin)).json()["data"]
    endpoint, secret = state["model_upstream_url"], state["model_upstream_key"]
    draft = {
        "id": "verified",
        "provider": "openai_compatible",
        "protocol": "chat_completions",
        "base_url": endpoint,
        "api_key_env": "YUKI_VERIFIED_KEY",
        "model": "fixture-chat-a",
        "timeout_seconds": 2,
        "max_retries": 3,
        "default_temperature": 0.7,
        "default_max_output_tokens": 2048,
        "capabilities": ["tools"],
    }
    connection = {
        key: draft[key]
        for key in ("provider", "protocol", "base_url", "api_key_env", "timeout_seconds")
    }
    discovered = await client.post(
        "admin/v1/models/discover",
        headers=admin,
        json={
            "draft_connection": connection,
            "api_key": secret,
        },
    )
    assert discovered.status_code == 200
    assert [item["id"] for item in discovered.json()["data"]["models"]] == [
        "fixture-chat-a",
        "fixture-chat-b",
    ]
    tested = await client.post(
        "admin/v1/models/test",
        headers=admin,
        json={
            "draft_profile": draft,
            "api_key": secret,
        },
    )
    assert tested.status_code == 200 and tested.json()["data"]["ok"] is True
    assert (await client.get("admin/v1/models", headers=admin)).json()["data"] == before
    assert (await client.get("admin/v1/models/stats", headers=admin)).json()["data"] == {
        "by_task": {},
        "by_profile": {},
    }
    denied = await client.post(
        "admin/v1/models/test",
        headers=visitor,
        json={
            "draft_profile": draft,
            "api_key": secret,
        },
    )
    assert denied.status_code == 403
    code = await client.post(
        "client/v1/link-codes",
        headers=admin,
        json={
            "request_id": str(uuid4()),
            "purpose": "connector",
        },
    )
    connector = await client.post(
        "agents/v1/pair",
        json={
            "code": code.json()["data"]["code"],
            "device_label": "Model credential boundary",
        },
    )
    rejected = await client.post(
        "admin/v1/models/test",
        headers={
            "Authorization": "Bearer " + connector.json()["token"],
        },
        json={"draft_profile": draft, "api_key": secret},
    )
    assert rejected.status_code == 401
    document = before["document"]
    document["profiles"]["verified"] = {key: value for key, value in draft.items() if key != "id"}
    document["embedding"] = {
        "enabled": True,
        "provider": "openai_compatible",
        "base_url": endpoint,
        "api_key_env": "YUKI_VERIFIED_KEY",
        "model": "fixture-embedding",
        "dimensions": 8,
    }
    request_id = str(uuid4())
    saved = await client.post(
        "admin/v1/models/apply",
        headers=admin,
        json={
            "request_id": request_id,
            "expected_revision": before["revision"],
            "document": document,
            "api_keys": {"YUKI_VERIFIED_KEY": secret},
        },
    )
    assert saved.status_code == 200 and saved.json()["data"]["success"] is True
    after = (await client.get("admin/v1/models", headers=admin)).json()["data"]
    assert after["revision"] != before["revision"]
    assert "verified" in after["saved_api_key_profiles"]
    assert after["document"]["embedding"]["dimensions"] == 8
    assert after["saved_connection_keys"] == ["embedding"]
    assert after["apply_mode"] == "restart"
    assert (
        await client.post("admin/v1/models/test", headers=admin, json={"profile_id": "verified"})
    ).json()["data"]["ok"] is True
    calls = (await client.get("__test/model-upstream-calls")).json()
    assert [call["method"] for call in calls] == ["GET", "POST", "POST"]
    assert all(call["has_authorization"] and call["authorized"] for call in calls)
    evidence = {
        "person_id": state["owner_person_id"],
        "device_id": device_id,
        "request_id": request_id,
        "before_revision": before["revision"],
        "after": after,
        "discover": discovered.json(),
        "test": tested.json(),
        "visitor_status": denied.status_code,
        "connector_status": rejected.status_code,
        "upstream_calls": calls,
    }
    encoded = json.dumps(evidence, ensure_ascii=False, indent=2)
    assert secret not in encoded and secret not in (root / "server.log").read_text("utf-8")
    artifact = Path("test-results/model-connections")
    await asyncio.to_thread(artifact.mkdir, parents=True, exist_ok=True)
    (artifact / "person-device-flow.json").write_text(encoded, encoding="utf-8")
