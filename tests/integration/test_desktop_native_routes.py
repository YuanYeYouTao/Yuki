"""Personal HTTP projections use the real TLS connector and original operation service."""

import asyncio
import json
from uuid import uuid4

import pytest
from tests.integration.test_external_agent_bridge import NativeComputer, accepted_work, directory
from tests.integration.test_external_agent_bridge import bridge as bridge_fixture
from tests.support.canonical_identity import ensure_person
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosedError

bridge = bridge_fixture


@pytest.mark.parametrize("completed", [False, True])
async def test_original_operation_http_keeps_work_link_for_pending_and_completed_receipts_and_acl(
    bridge, completed
):
    computer = await NativeComputer(bridge).start()
    try:
        instance, session = await directory(bridge)
        operation = str(uuid4())
        async with accepted_work(bridge) as control:
            result = await bridge.service.execute(
                {
                    "action": "prompt",
                    "instance_id": instance,
                    "session_id": session,
                    "prompt": "Only the original work",
                },
                operation,
                bridge.source,
            )
            work_id = control.current["id"]
        if completed:
            await computer.event(
                operation, "completed", {"status": "completed", "text": "Original result"}
            )
        response = await bridge.http.get(
            f"/agents/v1/operations/{operation}", headers=bridge.headers
        )
        assert response.status_code == 200, response.text
        view = response.json()
        assert view["work_id"] == work_id
        assert view["operation_id"] == operation and view["run_id"] == result["run_id"]
        assert view["session_id"] == session
        assert view["pending"] is not completed
        assert view["status"] == ("succeeded" if completed else "running")
        async with bridge.database.immediate_session() as writer:
            other_person = await ensure_person(writer, "native-http-" + str(uuid4()))
        code = await bridge.chat.issue_for_operator(other_person, str(uuid4()), str(uuid4()))
        other = await bridge.chat.pair(code["code"], "Other actual Person")
        denied = await bridge.http.get(
            f"/agents/v1/operations/{operation}",
            headers={"Authorization": "Bearer " + other["token"]},
        )
        missing = await bridge.http.get(
            f"/agents/v1/operations/{uuid4()}",
            headers={"Authorization": "Bearer " + other["token"]},
        )
        assert denied.status_code == missing.status_code == 404
        assert denied.json() == missing.json()
        connector = await bridge.http.get(
            f"/agents/v1/operations/{operation}",
            headers={"Authorization": "Bearer " + bridge.device["token"]},
        )
        assert connector.status_code == 401
        assert len([item for item in computer.commands if item["action"] == "prompt"]) == 1
    finally:
        await computer.close()


async def test_session_http_pagination_uses_internal_ids_without_creating_operations(bridge):
    computer = await NativeComputer(bridge).start()
    try:
        catalog = (await bridge.http.get("/agents/v1/devices", headers=bridge.headers)).json()
        instance = catalog["instances"][0]["id"]
        path = f"/agents/v1/instances/{instance}/sessions"
        first = await bridge.http.get(path, headers=bridge.headers)
        assert first.status_code == 200, first.text
        first = first.json()
        assert first["sessions"][0]["id"] != computer.native_session
        next_page = await bridge.http.get(
            path, params={"cursor": first["next_cursor"]}, headers=bridge.headers
        )
        assert next_page.status_code == 200
        assert next_page.json()["sessions"][0]["id"] != first["sessions"][0]["id"]
        assert next_page.json()["next_cursor"] is None
        assert (await bridge.http.get("/agents/v1/operations", headers=bridge.headers)).json() == {
            "items": [],
            "next_cursor": None,
        }
        assert (
            await bridge.http.get(
                path, headers={"Authorization": f"Bearer {bridge.device['token']}"}
            )
        ).status_code == 401
        assert (
            await bridge.http.get(
                f"/agents/v1/instances/{uuid4()}/sessions", headers=bridge.headers
            )
        ).status_code == 404
        assert all(command["action"] == "list" for command in computer.commands)
    finally:
        await computer.close()


async def test_original_control_receipt_and_work_operation_projection(bridge):
    computer = await NativeComputer(bridge).start()
    try:
        instance, session = await directory(bridge)
        operation = str(uuid4())
        async with accepted_work(bridge) as control:
            await bridge.service.execute(
                {"action": "watch", "instance_id": instance, "session_id": session},
                operation,
                bridge.source,
            )
            work_id = control.current["id"]
        source = bridge.source
        command = {"action": "detach", "operation_id": operation}
        identity = str(uuid4())
        original = await bridge.service.execute(command, identity, source)
        replay = await bridge.service.execute(command, identity, source)
        assert replay == original
        assert [row["action"] for row in computer.commands].count("detach") == 1
        listing = await bridge.http.get(
            "/agents/v1/operations", params={"work_id": work_id}, headers=bridge.headers
        )
        assert listing.status_code == 200, listing.text
        assert any(row["operation_id"] == operation for row in listing.json()["items"])
        found = await bridge.http.get(f"/agents/v1/operations/{operation}", headers=bridge.headers)
        assert found.status_code == 200, found.text
        assert found.json()["operation_id"] == operation
        first = (
            await bridge.http.get(
                "/agents/v1/operations", params={"limit": 1}, headers=bridge.headers
            )
        ).json()
        assert first["next_cursor"] == first["items"][0]["operation_id"]
        second = (
            await bridge.http.get(
                "/agents/v1/operations",
                params={"limit": 1, "cursor": first["next_cursor"]},
                headers=bridge.headers,
            )
        ).json()
        assert second["next_cursor"] is None
        assert second["items"][0]["operation_id"] != first["items"][0]["operation_id"]

    finally:
        await computer.close()


async def test_empty_catalog_retains_person_device_and_real_connection_state(bridge):
    before = (await bridge.http.get("/agents/v1/devices", headers=bridge.headers)).json()
    assert before["connector_protocol"] == 3
    assert before["instances"] == []
    assert before["devices"][0]["id"] == bridge.device["device_id"]
    assert before["devices"][0]["online"] is False
    async with connect(
        bridge.websocket_url,
        ssl=bridge.ssl,
        proxy=None,
        additional_headers={"Authorization": f"Bearer {bridge.device['token']}"},
    ) as socket:
        await socket.send(json.dumps({"type": "hello", "protocol_version": 3, "instances": []}))
        await socket.send(json.dumps({"type": "ping"}))
        assert json.loads(await socket.recv())["type"] == "pong"
        active = (await bridge.http.get("/agents/v1/devices", headers=bridge.headers)).json()
        assert active["instances"] == []
        assert active["devices"][0]["online"] is True


async def test_protocol_mismatch_returns_actual_close_reason(bridge):
    async with connect(
        bridge.websocket_url,
        ssl=bridge.ssl,
        proxy=None,
        additional_headers={"Authorization": f"Bearer {bridge.device['token']}"},
    ) as socket:
        await socket.send(json.dumps({"type": "hello", "protocol_version": 1, "instances": []}))
        with pytest.raises(ConnectionClosedError) as error:
            await socket.recv()
        assert error.value.rcvd.code == 1008
        assert error.value.rcvd.reason == "unsupported_connector_protocol"
    directory = (await bridge.http.get("/agents/v1/devices", headers=bridge.headers)).json()
    assert directory["devices"][0]["online"] is False
    assert directory["connector_protocol"] == 3


async def test_empty_device_transport_changes_wake_existing_hint_without_work(bridge, monkeypatch):
    hints = bridge.database.commit_hints
    queue = hints.subscribe()
    attach_entered, attach_release = asyncio.Event(), asyncio.Event()
    original_attach = bridge.service.connections.attach

    async def attach(connection):
        attach_entered.set()
        await attach_release.wait()
        await original_attach(connection)

    monkeypatch.setattr(bridge.service.connections, "attach", attach)
    try:
        async with connect(
            bridge.websocket_url,
            ssl=bridge.ssl,
            proxy=None,
            additional_headers={"Authorization": f"Bearer {bridge.device['token']}"},
        ) as socket:
            await socket.send(json.dumps({"type": "hello", "protocol_version": 3, "instances": []}))
            await asyncio.wait_for(attach_entered.wait(), 5)
            # The catalog transaction has committed while the transport is still offline.
            while not queue.empty():
                queue.get_nowait()
            offline = (await bridge.http.get("/agents/v1/devices", headers=bridge.headers)).json()
            assert offline["devices"][0]["online"] is False
            attach_release.set()
            assert await asyncio.wait_for(queue.get(), 5) is True
            await socket.send(json.dumps({"type": "ping"}))
            assert json.loads(await socket.recv())["type"] == "pong"
            online = (await bridge.http.get("/agents/v1/devices", headers=bridge.headers)).json()
            assert online["devices"][0]["online"] is True
        assert await asyncio.wait_for(queue.get(), 5) is True
        offline = (await bridge.http.get("/agents/v1/devices", headers=bridge.headers)).json()
        assert offline["devices"][0]["online"] is False
        assert (await bridge.http.get("/agents/v1/operations", headers=bridge.headers)).json() == {
            "items": [],
            "next_cursor": None,
        }
    finally:
        attach_release.set()
        hints.unsubscribe(queue)
