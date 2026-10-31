"""B-1 failures: current superuser chat credentials must enter the existing
admin capability plane; connector, ordinary and revoked credentials cannot.
Operator credentials keep their explicit grants and original failure codes.
"""

import asyncio
import json
from uuid import uuid4

from tests.integration.test_person_authority_http import (
    authority_http as authority_fixture,
)
from tests.integration.test_person_authority_http import (
    pair,
)

authority_http = authority_fixture


async def test_person_chat_credentials_use_current_admin_capabilities(authority_http):
    client, state, root = authority_http
    admin, first_id = await pair(client, state["owner_pairing_code"])
    ordinary, _ = await pair(client, state["nonowner_pairing_code"])
    second, second_id = await pair(client, (await client.post("__test/link-code")).json()["code"])
    operator = {"Authorization": "Bearer " + state["operator_token"]}
    limited = {"Authorization": "Bearer " + state["limited_operator_token"]}
    disabled = {"Authorization": "Bearer " + state["disabled_operator_token"]}
    original = await client.get("admin/v1/models", headers=admin)
    assert original.status_code == 200, original.text
    bundle = original.json()["data"]
    assert (await client.get("admin/v1/models", headers=second)).json()["data"] == bundle
    assert (await client.get("admin/v1/models", headers=ordinary)).status_code == 403
    assert (await client.get("admin/v1/models", headers=limited)).status_code == 403
    assert (await client.get("admin/v1/models", headers=disabled)).status_code == 409
    assert (await client.get("admin/v1/models", headers=operator)).status_code == 200
    connector_code = await client.post(
        "client/v1/link-codes",
        headers=admin,
        json={"request_id": str(uuid4()), "purpose": "connector"},
    )
    assert connector_code.status_code == 200, connector_code.text
    connector = await client.post(
        "agents/v1/pair",
        json={
            "code": connector_code.json()["data"]["code"],
            "device_label": "Connector auth boundary",
        },
    )
    assert connector.status_code == 200, connector.text
    assert (
        await client.get(
            "admin/v1/models",
            headers={
                "Authorization": "Bearer " + connector.json()["token"],
            },
        )
    ).status_code == 401
    payload = {
        "request_id": str(uuid4()),
        "expected_revision": bundle["revision"],
        "document": bundle["document"],
    }
    saved = await client.post("admin/v1/models/apply", headers=admin, json=payload)
    assert saved.status_code == 200, saved.text
    assert saved.json()["data"]["success"] is True
    replay = await client.post("admin/v1/models/apply", headers=second, json=payload)
    assert replay.status_code == 200, replay.text
    assert replay.json()["data"]["audit_id"] == saved.json()["data"]["audit_id"]
    original_file = (root / "server/models.toml").read_bytes()

    async def pending_save(headers):
        current = (await client.get("admin/v1/models", headers=headers)).json()["data"]
        await client.post("__test/hold-model-apply")
        operation = asyncio.create_task(
            client.post(
                "admin/v1/models/apply",
                headers=headers,
                json={
                    "request_id": str(uuid4()),
                    "expected_revision": current["revision"],
                    "document": current["document"],
                },
            )
        )
        assert (await client.get("__test/model-apply-started")).json() == {"started": True}
        return operation

    pending = await pending_save(admin)
    assert (
        await client.post("__test/revoke-device", json={"device_id": first_id})
    ).status_code == 200
    await client.post("__test/release-model-apply")
    denied_device = await pending
    assert denied_device.status_code == 401, denied_device.text
    assert (root / "server/models.toml").read_bytes() == original_file
    pending = await pending_save(second)
    await client.post(
        "__test/superuser", json={"person_id": state["owner_person_id"], "enabled": False}
    )
    await client.post("__test/release-model-apply")
    denied_person = await pending
    assert denied_person.status_code == 403, denied_person.text
    assert (root / "server/models.toml").read_bytes() == original_file
    assert (await client.get("admin/v1/models", headers=second)).status_code == 403
    assert (await client.get("admin/v1/models", headers=operator)).status_code == 200
    assert (await client.get("client/v1/me", headers=second)).json()["data"][
        "is_superuser"
    ] is False
    (root / "admin-authority-evidence.json").write_text(
        json.dumps(
            {
                "person_id": state["owner_person_id"],
                "device_ids": [first_id, second_id],
                "request_id": payload["request_id"],
                "audit_id": saved.json()["data"]["audit_id"],
                "revoked_device_during_command": denied_device.status_code,
                "revoked_person_during_command": denied_person.status_code,
                "operator_after_person_revoke": 200,
                "connector_status": 401,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
