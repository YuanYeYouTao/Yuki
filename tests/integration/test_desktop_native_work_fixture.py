"""Real App/Main/Work/HTTP with an explicit non-model native protocol peer."""

import asyncio
import contextlib
import json
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from tests.integration.test_external_agent_bridge import NativeComputer
from tests.support.desktop_native_work_fixture import DesktopNativeWorkScenario
from tests.support.soul_reference_server import serve

from yuki.runtime.work_activation import current_work_control
from yuki.runtime.work_control import WorkControl


@pytest.fixture
async def native_app(tmp_path, monkeypatch):
    root, state_file = tmp_path / "server", tmp_path / "ready.json"
    (root / "character").mkdir(parents=True)
    (root / "character" / "SOUL.md").write_text("你是 {{name}}。\n", encoding="utf-8")
    monkeypatch.chdir(root)
    monkeypatch.setenv("YUKI_DESKTOP_NATIVE_FIXTURE", "1")
    serving = asyncio.create_task(serve(root, state_file, 0, "/role/native", desktop=True))
    peer = None
    try:
        async with asyncio.timeout(30):
            while not state_file.exists():
                if serving.done():
                    serving.result()
                await asyncio.sleep(0.05)
        state = json.loads(state_file.read_text("utf-8"))
        async with httpx.AsyncClient(base_url=state["base_url"] + "/", trust_env=False) as client:
            paired = await client.post(
                "client/v1/pair",
                json={
                    "code": state["owner_pairing_code"],
                    "device_label": "Native UI fixture control",
                },
            )
            assert paired.status_code == 200, paired.text
            headers = httpx.Headers({"Authorization": "Bearer " + paired.json()["data"]["token"]})
            grant = await client.post(
                "client/v1/link-codes",
                headers=headers,
                json={
                    "request_id": str(uuid4()),
                    "purpose": "connector",
                },
            )
            assert grant.status_code == 200, grant.text
            connector = await client.post(
                "agents/v1/pair",
                json={
                    "code": grant.json()["data"]["code"],
                    "device_label": "Explicit protocol peer",
                },
            )
            assert connector.status_code == 200, connector.text
            device = connector.json()
            peer = await NativeComputer(
                SimpleNamespace(
                    websocket_url=state["base_url"].replace("http:", "ws:") + "/agents/v1/connect",
                    ssl=None,
                    device=device,
                )
            ).start()
            yield SimpleNamespace(
                client=client, headers=headers, state=state, peer=peer, device=device
            )
            assert (await client.post("__test/shutdown")).status_code == 200
            await asyncio.wait_for(serving, 10)
    finally:
        if peer is not None:
            await peer.close()
        if not serving.done():
            serving.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await serving


@pytest.fixture
async def permission_gate(native_app, permission, monkeypatch):
    committed, release = asyncio.Event(), asyncio.Event()
    gate = SimpleNamespace(committed=committed, release=release, control=None, response=None)
    original = WorkControl._control
    original_response = DesktopNativeWorkScenario.respond

    async def accept(control, args, call_key, **payload):
        result = await original(control, args, call_key, **payload)
        if permission and args.get("action") == "wait" and gate.control is None:
            gate.control = control
            committed.set()
            await release.wait()
        return result

    async def observe(scenario, request):
        result = await original_response(scenario, request)
        if result is not None and result.tool_calls:
            tool = result.tool_calls[0].function
            if tool.name == "external_agent" and json.loads(tool.arguments)["action"] == "respond":
                control = current_work_control.get()
                gate.response = SimpleNamespace(
                    trigger_event_id=control.source["trigger_event_id"],
                    work_id=control.current["id"],
                    input_ids=tuple(control.session.input_ids),
                )
        return result

    monkeypatch.setattr(WorkControl, "_control", accept)
    monkeypatch.setattr(DesktopNativeWorkScenario, "respond", observe)
    try:
        yield gate
    finally:
        release.set()


async def eventually(read, predicate):
    async with asyncio.timeout(15):
        while True:
            result = await read()
            if predicate(result):
                return result
            await asyncio.sleep(0.02)


@pytest.mark.parametrize(
    "terminal,expected,permission",
    [
        ("completed", "succeeded", False),
        ("failed", "failed", False),
        ("completed", "succeeded", True),
    ],
)
async def test_app_native_original_work_waits_for_real_receipt_and_never_reprompts(
    native_app, terminal, expected, permission, permission_gate
):
    f = native_app
    marker = "native-fixture-" + uuid4().hex
    assert (
        await f.client.post("__test/native-scenario", json={"marker": marker})
    ).status_code == 200
    catalog = (await f.client.get("agents/v1/devices", headers=f.headers)).json()
    instance = catalog["instances"][0]["id"]
    request_id = str(uuid4())
    body = {
        "request_id": request_id,
        "text": marker,
        "agent_target": {
            "intent": "prompt",
            "device_id": f.device["device_id"],
            "instance_id": instance,
            "cwd": "/isolated/native-project",
        },
    }
    first = await f.client.post("client/v1/messages", headers=f.headers, json=body)
    assert first.status_code == 200, first.text
    event_id = first.json()["data"]["event_id"]

    async def facts():
        result = await f.client.get("__test/native-facts")
        assert result.status_code == 200, result.text
        return result.json()

    if permission:
        await asyncio.wait_for(permission_gate.committed.wait(), 15)
        waiting = await facts()
        assert waiting["work"]["state"] == "running"
        assert (
            json.loads(waiting["work"]["checkpoint_json"])["accepted_control"]["action"] == "wait"
        )
    else:
        waiting = await eventually(
            facts, lambda v: v["work"] and v["work"]["state"] == "waiting_external"
        )
    prompt = [row for row in waiting["operations"] if row["action"] == "prompt"]
    assert len(prompt) == 1
    original = prompt[0]
    assert waiting["event_id"] == event_id
    assert original["actor_person_id"] == f.state["owner_person_id"]
    assert original["work_id"] == waiting["work"]["id"]
    assert original["task_status"] == "waiting" and original["completion"] is None
    assert original["command"]["arguments"]["prompt"] == marker
    assert len([row for row in f.peer.commands if row["action"] == "prompt"]) == 1
    replay = await f.client.post("client/v1/messages", headers=f.headers, json=body)
    assert replay.json()["data"]["replay"] is True
    assert replay.json()["data"]["event_id"] == event_id

    if permission:
        try:
            await f.peer.event(
                original["id"],
                "permission",
                {
                    "native_request_id": "original-native-permission",
                    "native_method": "item/commandExecution/requestApproval",
                    "native_request": {
                        "command": "isolated-probe",
                        "cwd": "/isolated/native-project",
                        "availableDecisions": ["accept", "decline"],
                    },
                },
            )
            view = (
                await f.client.get("agents/v1/operations/" + original["id"], headers=f.headers)
            ).json()
            question = next(row for row in view["events"] if row["kind"] == "permission")
            assert question["permission_id"] != "original-native-permission"
            response_body = {
                "request_id": str(uuid4()),
                "text": "Only approve the original isolated command",
                "agent_target": {
                    "intent": "respond",
                    "device_id": f.device["device_id"],
                    "instance_id": instance,
                    "operation_id": original["id"],
                    "permission_id": question["permission_id"],
                    "response": {"decision": "accept"},
                },
            }
            response = await f.client.post(
                "client/v1/messages", headers=f.headers, json=response_body
            )
            assert response.status_code == 200, response.text
            response_event_id = response.json()["data"]["event_id"]
        finally:
            permission_gate.release.set()

        async def responses():
            return [row for row in f.peer.commands if row["action"] == "respond"]

        native_response = (await eventually(responses, lambda rows: len(rows) == 1))[0]
        assert native_response["arguments"]["permission_id"] == "original-native-permission"
        assert native_response["arguments"]["response"] == {"decision": "accept"}
        assert permission_gate.response.trigger_event_id == event_id
        assert permission_gate.response.work_id == original["work_id"]
        reply_inputs = [
            row for row in (await facts())["inputs"] if row["event_id"] == response_event_id
        ]
        assert len(reply_inputs) == 1
        assert reply_inputs[0]["work_id"] == original["work_id"]
        assert reply_inputs[0]["state"] in {"staged", "consumed"}
        assert reply_inputs[0]["id"] in permission_gate.response.input_ids
        repeated = await f.client.post("client/v1/messages", headers=f.headers, json=response_body)
        assert repeated.json()["data"]["replay"] is True
        assert repeated.json()["data"]["event_id"] == response_event_id
        assert len(await responses()) == 1
        assert (await facts())["work"]["state"] not in {"completed", "failed", "cancelled"}

    prior_connection = waiting["connections"][0]["connection_id"]
    disconnected = await f.client.post(
        "__test/native-disconnect", json={"device_id": f.device["device_id"]}
    )
    assert disconnected.json()["connection_id"] == prior_connection
    await f.peer.reader
    await f.peer.start()
    reconnected = await eventually(
        facts,
        lambda v: v["connections"] and v["connections"][0]["connection_id"] != prior_connection,
    )
    assert reconnected["work"]["id"] == waiting["work"]["id"]
    await f.peer.event(original["id"], "progress", {"text": "Actual protocol milestone"})
    completed = await f.peer.event(
        original["id"], "completed", {"status": terminal, "text": marker}
    )
    final = await eventually(facts, lambda v: v["work"]["state"] == "completed")
    same = [row for row in final["operations"] if row["action"] == "prompt"]
    assert len(same) == 1 and same[0]["id"] == original["id"]
    assert same[0]["run_id"] == original["run_id"]
    assert same[0]["session_id"] == original["session_id"]
    assert same[0]["execution_id"] == original["execution_id"]
    assert same[0]["completion"]["status"] == expected
    assert final["work"]["goal"] == marker
    assert final["work"]["sent_messages"] == 1
    assert len(final["deliveries"]) == 1
    delivery = final["deliveries"][0]
    assert delivery["status"] == "succeeded"
    assert delivery["author_kind"] == "yuki" and delivery["direction"] == "outbound"
    assert (
        delivery["source_conversation_id"]
        == delivery["conversation_id"]
        == first.json()["data"]["conversation_id"]
    )
    assert delivery["original_event_id"] == delivery["event_id"]
    assert delivery["original_conversation_id"] == delivery["conversation_id"]
    assert json.loads(delivery["content"]) == {
        "status": expected,
        "text": marker,
        "operation_id": original["id"],
    }
    assert any(
        same[0]["run_id"] in row["payload_json"] and row["work_id"] == original["work_id"]
        for row in final["inputs"]
    )
    completion_events = [row for row in final["events"] if row["kind"] == "completed"]
    assert len(completion_events) == 1
    assert completion_events[0]["event_id"] == completed["event_id"]
    assert len([row for row in f.peer.commands if row["action"] == "prompt"]) == 1
    receipt = (await f.client.get("client/v1/requests/" + request_id, headers=f.headers)).json()[
        "data"
    ]
    assert receipt["found"] and receipt["event_id"] == event_id
    # Duplicate actual event UUID must not add a second completion or redispatch.
    await f.peer.event(
        original["id"], "completed", completed["payload"], event_id=completed["event_id"]
    )
    after_duplicate = await facts()
    assert len([row for row in after_duplicate["events"] if row["kind"] == "completed"]) == 1
    assert after_duplicate["inputs"] == final["inputs"]
