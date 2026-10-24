"""Late real Main activations cannot consume another Desktop Work scenario."""

import asyncio
import contextlib
import json
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from tests.support.desktop_fault_fixture import DesktopFaultFixture
from tests.support.soul_reference_server import serve

from yuki.llm.fake import FakeLLMProvider
from yuki.runtime.work_activation import current_work_control


async def eventually(read, predicate):
    async with asyncio.timeout(15):
        while True:
            value = await read()
            if predicate(value):
                return value
            await asyncio.sleep(0.02)


@pytest.fixture
async def work_app(tmp_path, monkeypatch):
    state_file = tmp_path / "ready.json"
    root = tmp_path / "server"
    (root / "character").mkdir(parents=True)
    (root / "character" / "SOUL.md").write_text("你是 {{name}}。\n", encoding="utf-8")
    monkeypatch.chdir(root)
    serving = asyncio.create_task(serve(root, state_file, 0, "/role/work", desktop=True))
    try:
        async with asyncio.timeout(15):
            while not state_file.exists():
                if serving.done():
                    serving.result()
                await asyncio.sleep(0.01)
        state = json.loads(state_file.read_text(encoding="utf-8"))
        async with httpx.AsyncClient(base_url=state["base_url"] + "/", trust_env=False) as client:
            paired = await client.post(
                "client/v1/pair",
                json={"code": state["owner_pairing_code"], "device_label": "Scenario regression"},
            )
            assert paired.status_code == 200, paired.text
            headers = httpx.Headers({"Authorization": "Bearer " + paired.json()["data"]["token"]})
            yield SimpleNamespace(client=client, headers=headers)
            assert (await client.post("__test/shutdown")).status_code == 200
        await asyncio.wait_for(serving, 10)
    finally:
        if not serving.done():
            serving.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await serving


async def post(f, path, body=None):
    response = await f.client.post(path, headers=f.headers, json=body)
    assert response.status_code == 200, response.text
    return response.json().get("data", response.json())


async def read(f, path):
    response = await f.client.get(path, headers=f.headers)
    assert response.status_code == 200, response.text
    return response.json()["data"]


async def waiting_work(f, event_id):
    value = await eventually(
        lambda: read(f, "client/v1/home"),
        lambda v: any(
            w["trigger_event_id"] == event_id and w["state"] == "waiting_user" for w in v["works"]
        ),
    )
    return next(w for w in value["works"] if w["trigger_event_id"] == event_id)


@pytest.mark.parametrize("old_kind", ["main", "work"])
async def test_late_activation_cannot_steal_new_scenario_and_reply_keeps_original_work(
    work_app, old_kind, monkeypatch
):
    f = work_app
    entered, release = asyncio.Event(), asyncio.Event()
    old_work_id = None
    old_request = None
    original_main = FakeLLMProvider._complete
    original_scenario = DesktopFaultFixture.respond
    armed = False

    async def main(self, request):
        nonlocal armed
        control = current_work_control.get()
        if armed and old_kind == "main" and control is not None and control.current is None:
            armed = False
            entered.set()
            await release.wait()
        return await original_main(self, request)

    async def scenario(self, request):
        nonlocal armed
        control = current_work_control.get()
        if (
            armed
            and old_kind == "work"
            and control is not None
            and control.current is not None
            and control.current["id"] == old_work_id
        ):
            armed = False
            entered.set()
            await release.wait()
        return await original_scenario(self, request)

    monkeypatch.setattr(FakeLLMProvider, "_complete", main)
    monkeypatch.setattr(DesktopFaultFixture, "respond", scenario)
    try:
        if old_kind == "work":
            await post(f, "__test/work-scenario")
            original = await post(
                f, "client/v1/messages", {"request_id": str(uuid4()), "text": "First real work"}
            )
            old_work = await waiting_work(f, original["event_id"])
            old_work_id = old_work["work_id"]
            armed = True
            old_request = str(uuid4())
            await post(
                f,
                f"client/v1/works/{old_work_id}/inputs",
                {"request_id": old_request, "text": "Original late reply"},
            )
        else:
            armed = True
            await post(
                f, "client/v1/messages", {"request_id": str(uuid4()), "text": "Old real Main"}
            )
        await asyncio.wait_for(entered.wait(), 10)
        await post(f, "__test/work-scenario")
        release.set()
        if old_work_id is not None:
            previous = await eventually(
                lambda: read(f, f"client/v1/works/{old_work_id}"),
                lambda w: w["state"] == "completed",
            )
            assert previous["trigger_event_id"] == original["event_id"]
            assert previous["generation"] == old_work["generation"]
            assert previous["budget"]["root_id"] == old_work["budget"]["root_id"]
            assert previous["budget"]["models"] >= old_work["budget"]["models"]
            assert previous["budget"]["tools"] >= old_work["budget"]["tools"]
            old_receipt = await read(f, f"client/v1/requests/{old_request}")
            assert old_receipt["status"] == "succeeded"

        request_id = str(uuid4())
        body = {"request_id": request_id, "text": "New independently prepared work"}
        created = await post(f, "client/v1/messages", body)
        work = await waiting_work(f, created["event_id"])
        assert work["work_id"] != old_work_id
        assert work["goal"] == "原工作整合測試"
        replay = await post(f, "client/v1/messages", body)
        assert replay == {**created, "replay": True}
        reply_id = str(uuid4())
        replied = await post(
            f,
            f"client/v1/works/{work['work_id']}/inputs",
            {"request_id": reply_id, "text": "台北，接續這項原工作"},
        )
        final = await eventually(
            lambda: read(f, f"client/v1/works/{work['work_id']}"),
            lambda w: w["state"] == "completed",
        )
        assert final["trigger_event_id"] == created["event_id"]
        assert final["work_id"] == work["work_id"]
        assert final["generation"] == work["generation"]
        assert final["budget"]["root_id"] == work["budget"]["root_id"]
        assert final["budget"]["models"] >= work["budget"]["models"]
        assert final["budget"]["tools"] >= work["budget"]["tools"]
        assert final["budget"]["model_limit"] == work["budget"]["model_limit"]
        assert final["budget"]["tool_limit"] == work["budget"]["tool_limit"]
        reply_receipt = await read(f, f"client/v1/requests/{reply_id}")
        assert reply_receipt["status"] == "succeeded"
        assert reply_receipt["result"]["event_id"] == replied["event_id"]
    finally:
        release.set()
