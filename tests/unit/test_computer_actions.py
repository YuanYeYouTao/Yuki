"""Manager side of computer/browser actions over the real runtime-file protocol.

Failure cases are listed first in docs/operations/computer-browser-sandbox-20261109.md
(C4–C9, P1, P2, P7). The Manager, its SQLite receipts and leases are real; the
environment driver is replaced by a thread that claims request files exactly as
``yuki_computer.py serve`` does, so crash and timeout points can be placed.
"""

import asyncio
import json
import os
import threading
import time
from pathlib import Path
from uuid import uuid4

import pytest
from tests.unit.test_computer_control import manager_at, set_control

from yuki.sandbox import computer_runtime


class Driver(threading.Thread):
    """Claim → answer, recording the lease file it saw before each "input"."""

    def __init__(self, root: Path, *, answer=True, claim=True):
        super().__init__(daemon=True)
        self.root, self.answer, self.claim = root, answer, claim
        self.seen: list[dict] = []
        self.active = 0
        self.overlap = False
        self.stopped = threading.Event()

    def run(self):
        requests = self.root / "driver/requests"
        while not self.stopped.is_set():
            for path in sorted(requests.glob("*.json")) if requests.is_dir() else ():
                if not self.claim:
                    continue
                taken = self.root / "driver/taken" / path.name
                taken.parent.mkdir(parents=True, exist_ok=True)
                try:
                    os.rename(path, taken)
                except FileNotFoundError:
                    continue
                request = json.loads(taken.read_text())
                self.active += 1
                self.overlap |= self.active > 1
                control = json.loads((self.root / "control.json").read_text())
                self.seen.append({"request": request, "control": control})
                time.sleep(0.05)
                self.active -= 1
                if not self.answer:
                    continue
                results = self.root / "driver/results"
                results.mkdir(parents=True, exist_ok=True)
                (results / (request["id"] + ".jpg")).write_bytes(b"\xff\xd8 fixture jpeg")
                body = {
                    "result": {"submitted": True, "width": 2, "height": 1},
                    "image": {
                        "file": request["id"] + ".jpg",
                        "media_type": "image/jpeg",
                        "width": 2,
                        "height": 1,
                    },
                }
                (results / ".tmp").write_text(json.dumps(body))
                os.replace(results / ".tmp", results / (request["id"] + ".json"))
                taken.unlink()
            time.sleep(0.01)


@pytest.fixture
def manager(tmp_path):
    value = manager_at(tmp_path)
    value.desktop.browser_status = lambda: "running"
    value.computer.deliver_results()
    yield value
    value.db.close()


@pytest.fixture
def driver(manager):
    started = []

    def start(**options):
        thread = Driver(manager.computer.root, **options)
        thread.start()
        started.append(thread)
        return thread

    yield start
    for thread in started:
        thread.stopped.set()
        thread.join(5)


def receipt(manager, key):
    row = manager.db.execute(
        "SELECT result FROM environment_mutations WHERE request_id=?", (key,)
    ).fetchone()
    return None if row is None else json.loads(row[0])


def click(owner="work-a"):
    return {"action": "click", "x": 1, "y": 1, "owner": owner, "max_dimension": 4096}


async def test_input_keeps_one_receipt_and_retry_never_drives_again(manager, driver):
    fake = driver()
    key = "call-" + str(uuid4())
    first = await manager.handle({"method": "computer_action", "args": click(), "request_id": key})
    assert first["submitted"] is True and first["image"]["width"] == 2
    # C4: the saved receipt holds no pixels and a retry returns it without driving.
    assert receipt(manager, key) == {"submitted": True, "width": 2, "height": 1}
    again = await manager.handle({"method": "computer_action", "args": click(), "request_id": key})
    assert again == {"submitted": True, "width": 2, "height": 1}
    assert len(fake.seen) == 1
    # The input ran under its own agent lease, released afterwards (C9).
    seen = fake.seen[0]
    assert seen["control"]["holder"] == "agent"
    assert seen["control"]["lease_id"] == seen["request"]["lease_id"]
    assert seen["request"]["args"] == {"action": "click", "x": 1, "y": 1}
    assert manager.computer.control() == {"holder": "none"}
    changed = {**click(), "x": 2}
    assert (
        await manager.handle({"method": "computer_action", "args": changed, "request_id": key})
    )["error"] == "idempotency_conflict"


async def test_reads_write_nothing_take_no_lease_and_work_while_a_human_holds(manager, driver):
    fake = driver()
    human = set_control(manager, "take")["control"]
    key = "call-" + str(uuid4())
    for _ in range(2):
        result = await manager.handle(
            {
                "method": "browser_action",
                "args": {"action": "snapshot", "owner": None},
                "request_id": key,
            }
        )
        assert result["submitted"] is True  # the fixture driver's answer, read twice
    assert receipt(manager, key) is None
    assert [item["request"]["lease_id"] for item in fake.seen] == [None, None]
    assert manager.computer.control() == human


async def test_human_holder_refuses_input_without_reaching_the_driver(manager, driver):
    fake = driver()
    set_control(manager, "take")
    key = "call-" + str(uuid4())
    result = await manager.handle({"method": "computer_action", "args": click(), "request_id": key})
    assert result["error"] == "control_held" and result["control"]["holder"] == "human"
    assert "status" not in result
    assert receipt(manager, key)["error"] == "control_held"
    assert fake.seen == []


async def test_pending_receipt_after_manager_end_is_unknown_and_never_run(
    manager, driver, monkeypatch
):
    key = "call-" + str(uuid4())
    requests = manager.computer.root / "driver/requests"
    original = manager.desktop._read_json

    def ended(path):
        if path.startswith("computer/driver/results/"):
            # The Manager ends after the pending row and the request file, before a driver.
            raise asyncio.CancelledError
        return original(path)

    monkeypatch.setattr(manager.desktop, "_read_json", ended)
    with pytest.raises(asyncio.CancelledError):
        await manager.desktop.action("computer_action", click(), key)
    monkeypatch.setattr(manager.desktop, "_read_json", original)
    assert receipt(manager, key) == {"pending": True}
    assert list(requests.glob("*.json"))
    result = await manager.handle({"method": "computer_action", "args": click(), "request_id": key})
    # C5/P1: unknown, and the leftover request is withdrawn before a driver starts.
    assert result["status"] == "unknown" and result["error"] == "action_unknown"
    fake = driver()
    await asyncio.sleep(0.2)
    assert fake.seen == [] and not list(requests.glob("*.json"))


async def test_unclaimed_timeout_did_not_run_claimed_timeout_is_unknown(
    manager, driver, monkeypatch
):
    monkeypatch.setattr(computer_runtime, "RESULT_SECONDS", 0.3)
    driver(claim=False)
    key = "call-" + str(uuid4())
    result = await manager.handle({"method": "computer_action", "args": click(), "request_id": key})
    # P2: never claimed → withdrawn → definitely not executed.
    assert result["error"] == "timeout" and "status" not in result
    assert not list((manager.computer.root / "driver/requests").glob("*.json"))
    assert manager.computer.control() == {"holder": "none"}
    driver(answer=False)
    key = "call-" + str(uuid4())
    result = await manager.handle({"method": "computer_action", "args": click(), "request_id": key})
    # Claimed but unanswered → the input may have happened (C6); lease released (C9).
    assert result["error"] == "timeout" and result["status"] == "unknown"
    assert receipt(manager, key)["status"] == "unknown"
    assert manager.computer.control() == {"holder": "none"}


async def test_concurrent_works_are_serialized_and_stale_agent_lease_does_not_block(
    manager, driver
):
    fake = driver()
    # An agent lease left by an ended Manager process (C7).
    set_control(manager, "agent_acquire", {"request_id": "dead-call"})
    results = await asyncio.gather(
        *(
            manager.handle(
                {
                    "method": "computer_action" if index % 2 else "browser_action",
                    "args": click(f"work-{index}")
                    if index % 2
                    else {"action": "click", "node": "e1", "owner": f"work-{index}"},
                    "request_id": f"call-{index}",
                }
            )
            for index in range(6)
        )
    )
    assert all(result.get("submitted") for result in results), results
    assert not fake.overlap
    leases = [item["control"]["lease_id"] for item in fake.seen]
    assert len(set(leases)) == 6
    assert all(item["control"]["holder"] == "agent" for item in fake.seen)
    assert manager.computer.control() == {"holder": "none"}


async def test_headless_has_no_screen_and_tabs_come_from_the_driver(manager):
    manager.desktop_manifest = {"desktop": False, "browser_mode": "headless", "tools": []}
    result = await manager.handle(
        {"method": "computer_action", "args": {"action": "screenshot"}, "request_id": "call-1"}
    )
    assert result["error"] == "display_unavailable"
    tabs = manager.computer.root / "driver/tabs.json"
    tabs.parent.mkdir(parents=True, exist_ok=True)
    tabs.write_text(json.dumps({"tabs": [{"target_id": "T", "url": "u", "work_id": "w"}]}))
    assert await manager.handle({"method": "tabs_list", "args": {}}) == {
        "tabs": [{"target_id": "T", "url": "u", "work_id": "w"}]
    }
