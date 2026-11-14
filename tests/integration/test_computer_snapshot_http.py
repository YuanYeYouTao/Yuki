"""Client API revision 12 over real HTTP: snapshots, restore and idle sleep on her computer.

The Desktop reference backend (tests/support/desktop_reference_server.py) with the
computer fixture: the Manager's own snapshot, restore and idle-sleep code against a
modelled Docker engine. Task book §7 and §11 S1, S2, S15 (confirmation), R1, R13.
Artifact: ``test-results/computer-snapshot-http.json``.
"""

import asyncio
import json
import os
import sys
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio

ARTIFACT = Path(__file__).resolve().parents[2] / "test-results" / "computer-snapshot-http.json"


@pytest_asyncio.fixture
async def backend(tmp_path):
    state_file = tmp_path / "state.json"
    log_file = tmp_path / "server.log"
    with log_file.open("wb") as log:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "tests.support.desktop_reference_server",
            "--root",
            str(tmp_path / "server"),
            "--state-file",
            str(state_file),
            env={
                **os.environ,
                "PYTHONPATH": os.pathsep.join(sys.path),
                "YUKI_DESKTOP_COMPUTER_FIXTURE": "1",
                "YUKI_DESKTOP_IDLE_MINUTES": "30",
            },
            stdout=log,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            async with asyncio.timeout(60):
                while not state_file.exists():
                    if process.returncode is not None:
                        pytest.fail(log_file.read_text(encoding="utf-8")[-6000:])
                    await asyncio.sleep(0.05)
            state = json.loads(state_file.read_text(encoding="utf-8"))
            async with httpx.AsyncClient(base_url=state["base_url"] + "/", timeout=30) as client:
                paired = await client.post(
                    "client/v1/pair",
                    json={"code": state["owner_pairing_code"], "device_label": "Snapshots"},
                )
                assert paired.status_code == 200, paired.text
                headers = {"Authorization": "Bearer " + paired.json()["data"]["token"]}
                yield client, headers
        finally:
            if process.returncode is None:
                process.terminate()
            await asyncio.wait_for(process.wait(), 15)


async def until(client, check, seconds=15):
    async with asyncio.timeout(seconds):
        while True:
            facts = (await client.get("__test/computer")).json()
            if check(facts):
                return facts
            await asyncio.sleep(0.1)


async def test_snapshot_restore_and_idle_sleep_over_client_api(backend):
    client, headers = backend
    record = {}
    meta = (await client.get("client/v1/meta")).json()["data"]
    assert meta["api_revision"] == 12 and "computer_snapshots" in meta["capabilities"]
    view = (await client.get("client/v1/computer", headers=headers)).json()["data"]
    assert view["snapshots"] == [] and view["snapshot_keep"] == 3
    assert view["idle_sleep_minutes"] == 30 and view["last_restore"] is None

    await client.post("__test/computer/marker", json={"text": "before"})
    first_id = str(uuid4())
    created = await client.post(
        "client/v1/computer/snapshots",
        headers=headers,
        json={"request_id": first_id, "note": "desk"},
    )
    assert created.status_code == 200, created.text
    snapshot = created.json()["data"]["snapshot"]
    assert snapshot["state"] == "creating" and snapshot["source"] == "person"
    assert snapshot["note"] == "desk" and snapshot["by"]["device_label"] == "Snapshots"
    facts = await until(client, lambda facts: facts["snapshots"][0]["state"] == "ready")
    # S1: the same personal request answers its original receipt, no second snapshot.
    again = await client.post(
        "client/v1/computer/snapshots",
        headers=headers,
        json={"request_id": first_id, "note": "desk"},
    )
    assert again.json()["data"]["snapshot"]["id"] == snapshot["id"]
    assert len((await client.get("__test/computer")).json()["snapshots"]) == 1
    view = (await client.get("client/v1/computer", headers=headers)).json()["data"]
    (listed,) = view["snapshots"]
    assert listed["id"] == snapshot["id"] and listed["state"] == "ready"
    assert listed["size_bytes"] > 0
    record["snapshot"] = listed

    # S2: one at a time.
    await client.post("__test/computer/snapshot-delay", json={"seconds": 2})
    slow = await client.post(
        "client/v1/computer/snapshots", headers=headers, json={"request_id": str(uuid4())}
    )
    busy = await client.post(
        "client/v1/computer/snapshots", headers=headers, json={"request_id": str(uuid4())}
    )
    assert busy.status_code == 409 and busy.json()["problem"]["code"] == "snapshot_in_progress"
    assert slow.status_code == 200 and busy.json()["problem"]["request_id"]
    await until(client, lambda facts: all(row["state"] != "creating" for row in facts["snapshots"]))
    await client.post("__test/computer/snapshot-delay", json={"seconds": 0})

    # S15: restoring names what it would interrupt; the person confirms.
    await client.post("__test/computer/marker", json={"text": "after"})
    restore_id = str(uuid4())
    payload = {"request_id": restore_id, "snapshot_id": snapshot["id"]}
    missing = await client.post(
        "client/v1/computer/restore",
        headers=headers,
        json={"request_id": str(uuid4()), "snapshot_id": str(uuid4()), "confirm_active_work": True},
    )
    assert missing.status_code == 404, missing.text
    accepted = await client.post(
        "client/v1/computer/restore", headers=headers, json={**payload, "confirm_active_work": True}
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["data"]["restore"]["state"] == "accepted"
    facts = await until(client, lambda facts: facts["restores"][0]["state"] == "done")
    assert facts["marker"] == "before"
    view = (await client.get("client/v1/computer", headers=headers)).json()["data"]
    assert view["last_restore"]["snapshot_id"] == snapshot["id"]
    assert view["last_restore"]["state"] == "done"
    record["restore"] = view["last_restore"]

    # R1 at the API: idle sleep stops the screen; the Desktop sees it in its next view.
    assert facts["display"] == "running"
    await client.post("__test/computer/idle")
    facts = await until(client, lambda facts: facts["display"] == "stopped")
    (receipt,) = facts["idle_receipts"]
    assert receipt["request_id"].startswith("idle:") and receipt["result"]["display"] == "stopped"
    view = (await client.get("client/v1/computer", headers=headers)).json()["data"]
    assert view["display"] == "stopped"
    woken = await client.post(
        "client/v1/computer/wake", headers=headers, json={"request_id": str(uuid4())}
    )
    assert woken.status_code == 200 and woken.json()["data"]["display"] == "running"
    record["idle_receipt"] = receipt

    # S32: once rotated out (three are kept), the original request still answers with
    # its own receipt; a Manager answer of state "rotated" is a valid view too.
    for _ in range(2):
        await client.post(
            "client/v1/computer/snapshots", headers=headers, json={"request_id": str(uuid4())}
        )
        await until(
            client, lambda facts: all(row["state"] != "creating" for row in facts["snapshots"])
        )
    rotated = await client.post(
        "client/v1/computer/snapshots",
        headers=headers,
        json={"request_id": first_id, "note": "desk"},
    )
    assert rotated.status_code == 200, rotated.text
    assert rotated.json()["data"]["snapshot"]["id"] == snapshot["id"]
    facts = (await client.get("__test/computer")).json()
    (first,) = [row for row in facts["snapshots"] if row["id"] == snapshot["id"]]
    assert first["state"] == "rotated"
    record["rotated_retry"] = rotated.json()["data"]
    ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
    ARTIFACT.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
