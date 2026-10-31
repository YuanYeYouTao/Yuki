"""B-2 failures: only an explicit grant request grants the selected Person;
old installations without one stay ordinary, and revoked grants never revive.
Creation and reconnect preserve original Person, pairing and grant receipts.
The real CLI and real HTTP operator API run only against this temporary Bot.
"""

import asyncio
import json
import os
import sys
from uuid import uuid4

from tests.integration.test_person_authority_http import (
    authority_http as authority_fixture,
)
from tests.integration.test_person_authority_http import pair

authority_http = authority_fixture


async def test_trusted_local_pair_grants_person_once_and_preserves_original_receipts(
    authority_http,
):
    client, state, root = authority_http
    operator = {"Authorization": "Bearer " + state["operator_token"]}

    async def local_pair(body, *, success=True):
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            "from yuki.cli import main; raise SystemExit(main())",
            "setup",
            "desktop-pair",
            "--deployment-root",
            str(root),
            env={
                **os.environ,
                "PYTHONPATH": os.pathsep.join(sys.path),
                "ADMIN_API_URL": state["base_url"],
                "YUKI_CONTROL_OPERATOR_TOKEN": state["operator_token"],
            },
            cwd=root,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        output, error = await process.communicate(json.dumps(body).encode())
        assert (process.returncode == 0) is success, error.decode()
        return json.loads(output) if success else None

    body = {
        "display_name": "Explicit local deployer",
        "person_request_id": str(uuid4()),
        "superuser_request_id": str(uuid4()),
        "pair_request_id": str(uuid4()),
    }
    issued = await local_pair(body)
    headers, device_id = await pair(client, issued["code"])
    me = (await client.get("client/v1/me", headers=headers)).json()["data"]
    assert me["person_id"] == issued["person_id"]
    assert me["is_superuser"] is True
    person_path = "admin/v1/persons/" + me["person_id"]
    person = (await client.get(person_path, headers=operator)).json()["data"]
    receipt_path = "admin/v1/operations/" + body["superuser_request_id"]
    original_receipt = (await client.get(receipt_path, headers=operator)).json()["data"]
    assert original_receipt["status"] == "succeeded"
    assert original_receipt["result_resource_id"] == me["person_id"]
    replay = await local_pair(body)
    assert replay["person_id"] == issued["person_id"] and replay["code_id"] == issued["code_id"]
    assert replay["code"] is None
    assert (await client.get(person_path, headers=operator)).json()["data"] == person
    assert (await client.get(receipt_path, headers=operator)).json()["data"] == original_receipt

    revoked = await client.post(
        person_path + "/superuser/revoke",
        headers=operator,
        json={"request_id": str(uuid4()), "expected_revision": person["revision"]},
    )
    assert revoked.status_code == 200, revoked.text
    revoked_person = (await client.get(person_path, headers=operator)).json()["data"]
    assert revoked_person["is_superuser"] is False
    assert (await local_pair(body))["code_id"] == issued["code_id"]
    assert (await client.get(person_path, headers=operator)).json()["data"] == revoked_person
    assert (await client.get("client/v1/me", headers=headers)).json()["data"][
        "is_superuser"
    ] is False
    reconnect = await local_pair({**body, "pair_request_id": str(uuid4())})
    reconnected, reconnected_device = await pair(client, reconnect["code"])
    reconnected_me = (await client.get("client/v1/me", headers=reconnected)).json()["data"]
    assert reconnected_me["person_id"] == me["person_id"]
    assert reconnected_me["is_superuser"] is False
    assert reconnected_device != device_id
    assert (await client.get(receipt_path, headers=operator)).json()["data"] == original_receipt
    assert (await client.get(person_path, headers=operator)).json()["data"] == revoked_person
    await local_pair({**body, "person_id": state["owner_person_id"]}, success=False)
    await local_pair({**body, "superuser_request_id": body["person_request_id"]}, success=False)
    assert (await client.get(person_path, headers=operator)).json()["data"] == revoked_person

    ordinary, _ = await pair(client, state["nonowner_pairing_code"])
    assert (await client.get("client/v1/me", headers=ordinary)).json()["data"][
        "is_superuser"
    ] is False
    existing = {
        "display_name": "Selected existing Person",
        "person_id": state["nonowner_person_id"],
        "person_request_id": str(uuid4()),
        "superuser_request_id": str(uuid4()),
        "pair_request_id": str(uuid4()),
    }
    ordinary_path = "admin/v1/persons/" + existing["person_id"]
    ordinary_person = (await client.get(ordinary_path, headers=operator)).json()["data"]
    missing = {key: value for key, value in existing.items() if key != "superuser_request_id"}
    old_installation = await local_pair(missing)
    old_headers, old_device = await pair(client, old_installation["code"])
    old_me = (await client.get("client/v1/me", headers=old_headers)).json()["data"]
    assert old_me["person_id"] == existing["person_id"]
    assert old_me["is_superuser"] is False
    assert (await client.get(ordinary_path, headers=operator)).json()["data"] == ordinary_person
    old_replay = await local_pair(missing)
    assert old_replay["code_id"] == old_installation["code_id"] and old_replay["code"] is None
    assert (await client.get(ordinary_path, headers=operator)).json()["data"] == ordinary_person
    existing["pair_request_id"] = str(uuid4())
    existing_pair = await local_pair(existing)
    existing_headers, _ = await pair(client, existing_pair["code"])
    assert (await client.get("client/v1/me", headers=existing_headers)).json()["data"][
        "is_superuser"
    ] is True
    current_owner = (
        await client.get("admin/v1/persons/" + state["owner_person_id"], headers=operator)
    ).json()["data"]
    already_superuser = {
        **existing,
        "person_id": state["owner_person_id"],
        "superuser_request_id": str(uuid4()),
        "pair_request_id": str(uuid4()),
    }
    await local_pair(already_superuser)
    assert (
        await client.get("admin/v1/persons/" + state["owner_person_id"], headers=operator)
    ).json()["data"] == current_owner
    ordinary_creation = await local_pair(
        {
            "display_name": "Local chat without an administrator grant",
            "person_request_id": str(uuid4()),
            "pair_request_id": str(uuid4()),
        }
    )
    created_headers, created_device = await pair(client, ordinary_creation["code"])
    created_me = (await client.get("client/v1/me", headers=created_headers)).json()["data"]
    assert created_me["person_id"] == ordinary_creation["person_id"]
    assert created_me["is_superuser"] is False
    (root / "local-superuser-evidence.json").write_text(
        json.dumps(
            {
                "person_id": me["person_id"],
                "device_id": device_id,
                "person_request_id": body["person_request_id"],
                "superuser_request_id": body["superuser_request_id"],
                "pair_request_id": body["pair_request_id"],
                "grant_receipt": original_receipt,
                "revoked_replay_is_superuser": revoked_person["is_superuser"],
                "existing_person_id": existing["person_id"],
                "ordinary_remote_pair_is_superuser": False,
                "revoked_reconnect_device_id": reconnected_device,
                "revoked_reconnect_is_superuser": reconnected_me["is_superuser"],
                "old_installation_person_id": old_me["person_id"],
                "old_installation_device_id": old_device,
                "old_installation_pair_request_id": missing["pair_request_id"],
                "old_installation_code_id": old_installation["code_id"],
                "old_installation_person_unchanged": True,
                "old_installation_is_superuser": old_me["is_superuser"],
                "ordinary_creation_person_id": created_me["person_id"],
                "ordinary_creation_device_id": created_device,
                "ordinary_creation_is_superuser": created_me["is_superuser"],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
