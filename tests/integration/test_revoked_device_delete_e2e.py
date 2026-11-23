"""Deleting a revoked device removes only its credential row; original facts stay.

Failure cases written before the implementation:

1. no bearer, or the deleted device's own revoked token -> 401;
2. another Person's device, or an unknown ID -> the same 404;
3. a device that is still active -> 409 precondition_failed, and it keeps working;
4. an operator without ``identity.client.revoke`` -> 403 capability_denied;
5. a revoked connector device anchors native instances/events (FK CASCADE) -> 409 and
   its native rows stay;
6. revoked chat device -> deleted and gone from ``/me``; the same request ID replays the
   original result, a new request ID finds nothing;
7. the deleted device's chat events, request receipts, audits and link codes stay
   byte-for-byte, and the person's other device still finds the original request.
"""

import asyncio
import hashlib
import json
import subprocess
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select, text
from tests.integration.test_acp_runtime_integration import sent_reply
from tests.integration.test_current_material_main_contract import _deployment
from tests.unit.test_client_api import bearer

from yuki.admin_api.http import attach_admin_api
from yuki.client_api.http import attach_client_api
from yuki.external_agents.identity import ConnectorIdentityService
from yuki.external_agents.repository import AgentRepository
from yuki.llm.fake import FakeLLMProvider

FULL = "device-delete-operator-" + "f" * 40
READER = "device-delete-reader-" + "r" * 40


def operators(tmp_path):
    path = tmp_path / "operators.toml"
    path.write_text(
        "".join(
            "[[operators]]\n"
            f'principal_id = "{uuid4()}"\ntoken_env = "{env}"\n'
            'enabled = true\nroles = ["superuser"]\n'
            f"capabilities = {json.dumps(capabilities)}\n"
            for env, capabilities in (
                (
                    "YUKI_TEST_DELETE_OPERATOR",
                    ["identity.person.create", "identity.client.pair", "identity.client.revoke"],
                ),
                ("YUKI_TEST_DELETE_READER", ["identity.person.create"]),
            )
        ),
        encoding="utf-8",
    )
    return path


async def history(database, device_id, conversation_id):
    """Every stored fact that names the device or belongs to its conversation."""
    queries = {
        "chat_events": (
            "SELECT * FROM chat_events WHERE canonical_conversation_id = :c ORDER BY id",
            {"c": conversation_id},
        ),
        "control_command_receipts": (
            "SELECT * FROM control_command_receipts WHERE principal_id = :d ORDER BY id",
            {"d": device_id},
        ),
        "admin_operation_events": (
            "SELECT * FROM admin_operation_events WHERE actor_principal_id = :d ORDER BY id",
            {"d": device_id},
        ),
        "client_link_codes": (
            "SELECT * FROM client_link_codes WHERE issuer_principal_id = :d ORDER BY id",
            {"d": device_id},
        ),
    }
    async with database.read_session() as reader:
        return {
            name: [
                {key: str(value) for key, value in row._mapping.items()}
                for row in await reader.execute(text(sql), parameters)
            ]
            for name, (sql, parameters) in queries.items()
        }


async def settled(http, token, request_id):
    async with asyncio.timeout(15):
        while True:
            response = await http.get(f"/client/v1/requests/{request_id}", headers=bearer(token))
            assert response.status_code == 200, response.text
            facts = response.json()["data"]
            if facts.get("turn", {}).get("state") == "ended":
                return facts
            await asyncio.sleep(0.01)


@pytest.fixture
def evidence(request):
    """Write the scenario's actual responses even when an assertion fails."""
    root = request.config.rootpath
    collected: dict = {"steps": []}
    try:
        yield collected
    finally:
        output = root / "test-results" / "revoked-device-delete"
        output.mkdir(parents=True, exist_ok=True)
        revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True)
        (output / "evidence.json").write_text(
            json.dumps(
                {"nodeid": request.node.nodeid, "source_revision": revision.strip(), **collected},
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )


async def test_only_revoked_chat_devices_are_deleted_and_original_facts_stay(
    database, tmp_path, monkeypatch, evidence
):
    monkeypatch.setenv("YUKI_TEST_DELETE_OPERATOR", FULL)
    monkeypatch.setenv("YUKI_TEST_DELETE_READER", READER)
    app, *_ = await _deployment(
        database, tmp_path, FakeLLMProvider(sent_reply), control_operators_file=operators(tmp_path)
    )

    def step(name, response, expected):
        evidence["steps"].append(
            {"step": name, "status": response.status_code, "body": response.json()}
        )
        assert response.status_code == expected, (name, response.text)
        return response.json()

    try:
        await app.app_gateway.start()
        await app._freeze_main_manifest()
        http_app = FastAPI()
        attach_admin_api(http_app, lambda: app.admin_api)
        attach_client_api(http_app, lambda: app.client_api)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=http_app), base_url="http://test"
        ) as http:

            async def person(name):
                created = await http.post(
                    "/admin/v1/persons",
                    headers=bearer(FULL),
                    json={"request_id": str(uuid4()), "expected_revision": 0, "display_name": name},
                )
                assert created.status_code == 200, created.text
                return created.json()["data"]["resource_id"]

            async def pair(person_id, label):
                issued = await http.post(
                    f"/admin/v1/persons/{person_id}/client-pair",
                    headers=bearer(FULL),
                    json={"request_id": str(uuid4())},
                )
                assert issued.status_code == 200, issued.text
                paired = await http.post(
                    "/client/v1/pair",
                    json={"code": issued.json()["data"]["code"], "device_label": label},
                )
                assert paired.status_code == 200, paired.text
                return paired.json()["data"]

            owner, stranger = await person("Device owner"), await person("Someone else")
            a = await pair(owner, "Phone A")
            linked = await http.post(
                "/client/v1/link-codes",
                headers=bearer(a["token"]),
                json={"request_id": str(uuid4())},
            )
            assert linked.status_code == 200, linked.text
            paired = await http.post(
                "/client/v1/pair",
                json={"code": linked.json()["data"]["code"], "device_label": "Laptop B"},
            )
            b = paired.json()["data"]
            other = await pair(stranger, "Other phone")

            message = str(uuid4())
            sent = await http.post(
                "/client/v1/messages",
                headers=bearer(a["token"]),
                json={"request_id": message, "text": "Sent from the phone before it was lost"},
            )
            assert sent.status_code == 200, sent.text
            facts = await settled(http, a["token"], message)
            assert facts["turn"]["outcome"] == "chat"
            conversation = sent.json()["data"]["conversation_id"]
            events = (
                await http.get(
                    f"/client/v1/conversations/{conversation}/events", headers=bearer(b["token"])
                )
            ).json()["data"]["items"]
            before = await history(database, a["device_id"], conversation)
            assert before["chat_events"] and before["control_command_receipts"]
            assert before["client_link_codes"]

            delete_a = f"/client/v1/devices/{a['device_id']}/delete"
            step("no bearer", await http.post(delete_a, json={"request_id": str(uuid4())}), 401)
            step(
                "another person's device",
                await http.post(
                    delete_a, headers=bearer(other["token"]), json={"request_id": str(uuid4())}
                ),
                404,
            )
            step(
                "unknown device",
                await http.post(
                    f"/client/v1/devices/{uuid4()}/delete",
                    headers=bearer(b["token"]),
                    json={"request_id": str(uuid4())},
                ),
                404,
            )
            active = step(
                "active device",
                await http.post(
                    delete_a, headers=bearer(b["token"]), json={"request_id": str(uuid4())}
                ),
                409,
            )
            assert active == {"problem": {"code": "precondition_failed"}}
            assert (await http.get("/client/v1/me", headers=bearer(a["token"]))).status_code == 200

            step(
                "revoke",
                await http.request(
                    "DELETE",
                    f"/client/v1/devices/{a['device_id']}",
                    headers=bearer(b["token"]),
                    json={"request_id": str(uuid4())},
                ),
                200,
            )
            step(
                "revoked device deletes itself",
                await http.post(
                    delete_a, headers=bearer(a["token"]), json={"request_id": str(uuid4())}
                ),
                401,
            )
            denied = step(
                "operator without identity.client.revoke",
                await http.post(
                    f"/admin/v1/devices/{a['device_id']}/delete",
                    headers=bearer(READER),
                    json={"request_id": str(uuid4())},
                ),
                403,
            )
            assert denied == {"problem": {"code": "capability_denied"}}
            step(
                "another person's revoked device",
                await http.post(
                    delete_a, headers=bearer(other["token"]), json={"request_id": str(uuid4())}
                ),
                404,
            )

            original = str(uuid4())
            deleted = step(
                "delete revoked device",
                await http.post(
                    delete_a, headers=bearer(b["token"]), json={"request_id": original}
                ),
                200,
            )
            assert deleted["data"] == {
                "device_id": a["device_id"],
                "deleted": True,
                "replay": False,
            }
            me = (await http.get("/client/v1/me", headers=bearer(b["token"]))).json()["data"]
            assert a["device_id"] not in {device["device_id"] for device in me["devices"]}
            assert b["device_id"] in {device["device_id"] for device in me["devices"]}
            replayed = step(
                "same request again",
                await http.post(
                    delete_a, headers=bearer(b["token"]), json={"request_id": original}
                ),
                200,
            )
            assert replayed["data"] == {**deleted["data"], "replay": True}
            step(
                "new request for the deleted device",
                await http.post(
                    delete_a, headers=bearer(b["token"]), json={"request_id": str(uuid4())}
                ),
                404,
            )

            after = await history(database, a["device_id"], conversation)
            assert after == before
            found = await http.get(f"/client/v1/requests/{message}", headers=bearer(b["token"]))
            assert found.status_code == 200 and found.json()["data"]["found"], found.text
            assert found.json()["data"]["event_id"] == facts["event_id"]
            assert (
                await http.get(
                    f"/client/v1/conversations/{conversation}/events", headers=bearer(b["token"])
                )
            ).json()["data"]["items"] == events

            c = await pair(owner, "Tablet C")
            delete_c = f"/admin/v1/devices/{c['device_id']}/delete"
            step(
                "operator deletes an active device",
                await http.post(delete_c, headers=bearer(FULL), json={"request_id": str(uuid4())}),
                409,
            )
            step(
                "operator revokes",
                await http.post(
                    f"/admin/v1/devices/{c['device_id']}/revoke",
                    headers=bearer(FULL),
                    json={"request_id": str(uuid4())},
                ),
                200,
            )
            operator_request = str(uuid4())
            removed = step(
                "operator deletes the revoked device",
                await http.post(
                    delete_c, headers=bearer(FULL), json={"request_id": operator_request}
                ),
                200,
            )
            assert removed["data"] == {
                "device_id": c["device_id"],
                "deleted": True,
                "replay": False,
            }
            again = step(
                "operator repeats the same request",
                await http.post(
                    delete_c, headers=bearer(FULL), json={"request_id": operator_request}
                ),
                200,
            )
            assert again["data"]["replay"] is True
            step(
                "operator new request for the deleted device",
                await http.post(delete_c, headers=bearer(FULL), json={"request_id": str(uuid4())}),
                404,
            )

            issued = await http.post(
                "/client/v1/link-codes",
                headers=bearer(b["token"]),
                json={"request_id": str(uuid4()), "purpose": "connector"},
            )
            connector = await ConnectorIdentityService(database).pair(
                issued.json()["data"]["code"], "Old computer"
            )
            await AgentRepository(database).register(
                connector["device_id"],
                [{"id": "codex", "label": "Codex", "driver": "codex", "status": "available"}],
            )
            step(
                "revoke connector",
                await http.request(
                    "DELETE",
                    f"/client/v1/devices/{connector['device_id']}",
                    headers=bearer(b["token"]),
                    json={"request_id": str(uuid4())},
                ),
                200,
            )
            kept = step(
                "revoked connector with native history",
                await http.post(
                    f"/client/v1/devices/{connector['device_id']}/delete",
                    headers=bearer(b["token"]),
                    json={"request_id": str(uuid4())},
                ),
                409,
            )
            assert kept == {"problem": {"code": "precondition_failed"}}
            async with database.read_session() as reader:
                instances = list(
                    await reader.scalars(
                        select(text("id"))
                        .select_from(text("external_agent_instances"))
                        .where(text("device_id = :d"))
                        .params(d=connector["device_id"])
                    )
                )
            assert len(instances) == 1
            evidence["history_sha256"] = hashlib.sha256(
                json.dumps(after, sort_keys=True).encode()
            ).hexdigest()
            evidence["history_rows"] = {name: len(rows) for name, rows in after.items()}
    finally:
        assert not await app.lifecycle._close_entries(app.lifecycle._entries)
