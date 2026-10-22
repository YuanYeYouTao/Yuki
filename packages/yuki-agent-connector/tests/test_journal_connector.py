from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve

from yuki_agent_connector.config import Config, Instance
from yuki_agent_connector.connector import Connector
from yuki_agent_connector.journal import Journal


def command(identity: str = "original", action: str = "prompt") -> dict[str, Any]:
    return {
        "type": "command",
        "id": identity,
        "instance_id": "local",
        "action": action,
        "arguments": {"prompt": "original input"},
    }


@pytest.fixture
def journal(tmp_path: Path) -> Any:
    value = Journal(tmp_path / "private" / "journal.db", namespace="server/device")
    yield value
    value.close()


def test_crash_window_remains_unknown_and_namespace_does_not_move(
    journal: Journal, tmp_path: Path
) -> None:
    original = command()
    assert journal.begin(original) is None
    assert journal.begin(original)["result"]["status"] == "unknown"  # type: ignore[index]
    with pytest.raises(ValueError, match="parameters"):
        journal.begin({**original, "arguments": {"prompt": "changed"}})
    with pytest.raises(ValueError, match="another server/device"):
        Journal(tmp_path / "private" / "journal.db", namespace="different owner")


def test_events_are_committed_before_send_and_ack_prunes_only_original(journal: Journal) -> None:
    original = command()
    journal.begin(original)
    journal.complete(
        original["id"],
        {
            "type": "response",
            "id": original["id"],
            "result": {
                "session_id": "native-session",
                "execution_id": "native-turn",
                "pending": True,
            },
        },
    )
    event = {
        "type": "event",
        "event_id": "e1",
        "kind": "completed",
        "operation_id": original["id"],
        "payload": {"status": "completed"},
    }
    journal.append(event)
    journal.append(event)
    assert journal.pending() == [event]
    journal.ack("another-event")
    assert journal.pending() == [event]
    journal.ack("e1")
    assert journal.pending() == []
    assert journal.operation(original["id"])["status"] == "completed"
    assert journal.operation(original["id"])["pending"] is False
    assert journal.observations() == []


class FakeDriver:
    def __init__(self) -> None:
        self.spec = Instance("local", "fake", "Fake", "fake")
        self.calls: list[dict[str, Any]] = []
        self.capabilities = ["list", "get", "prompt"]
        self.started = asyncio.Event()
        self.finish = asyncio.Event()

    async def execute(self, value: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(value)
        self.started.set()
        if value["action"] == "prompt":
            await self.finish.wait()
        return {"session_id": "native-session", "execution_id": "native-turn", "pending": True}

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def recover(self, value: dict[str, Any], result: dict[str, Any]) -> None:
        pass


@pytest.mark.asyncio
async def test_duplicate_original_and_parameter_mismatch_never_dispatch_twice(
    journal: Journal,
) -> None:
    driver = FakeDriver()
    connector = Connector(Config("ws://localhost/", "device", "not-a-real-token"), journal)
    connector.drivers["local"] = driver
    task = asyncio.create_task(connector.dispatch(command()))
    await driver.started.wait()
    duplicate = await connector.dispatch(command())
    assert duplicate["result"]["status"] == "unknown"
    mismatch = await connector.dispatch({**command(), "arguments": {"prompt": "changed"}})
    assert mismatch["error"]["code"] == "invalid_command"
    assert journal.operation("original")["status"] == "unknown"
    driver.finish.set()
    response = await task
    assert await connector.dispatch(command()) == response
    assert len(driver.calls) == 1
    assert journal.operation("original")["execution_id"] == "native-turn"


@pytest.mark.asyncio
async def test_queries_do_not_write_or_require_a_work(journal: Journal) -> None:
    connector = Connector(Config("ws://localhost/", "device", "token"), journal)
    connector.drivers["local"] = FakeDriver()
    before = journal.db.total_changes
    await connector.dispatch(command(action="list"))
    await connector.dispatch(command(action="get"))
    assert journal.db.total_changes == before


@pytest.mark.asyncio
async def test_network_disconnect_leaves_native_dispatch_running_and_replays_events(
    journal: Journal,
) -> None:
    connector = Connector(Config("ws://localhost/", "device", "token"), journal)
    driver = FakeDriver()
    connector.drivers["local"] = driver
    received: list[dict[str, Any]] = []
    connections = 0
    acked = asyncio.Event()

    async def server(socket: Any) -> None:
        nonlocal connections
        connections += 1
        received.append(json.loads(await socket.recv()))
        if connections == 1:
            await socket.send(json.dumps(command()))
            await driver.started.wait()
            await socket.close()
        else:
            event = json.loads(await socket.recv())
            received.append(event)
            await socket.send(json.dumps({"type": "ack", "event_id": event["event_id"]}))
            acked.set()
            await socket.wait_closed()

    async with serve(server, "127.0.0.1", 0) as listener:
        url = f"ws://127.0.0.1:{listener.sockets[0].getsockname()[1]}"
        async with connect(url) as socket:
            await connector.connection(socket)
        assert not driver.finish.is_set()
        driver.finish.set()
        await asyncio.gather(*connector.tasks)
        journal.append(
            {
                "type": "event",
                "event_id": "offline-event",
                "kind": "completed",
                "operation_id": "original",
                "payload": {"status": "completed"},
            }
        )
        async with connect(url) as socket:
            task = asyncio.create_task(connector.connection(socket))
            await asyncio.wait_for(acked.wait(), 2)
            await socket.close()
            await task
    assert len(driver.calls) == 1
    assert received[-1]["event_id"] == "offline-event"
    assert journal.pending() == []


def test_detach_only_removes_its_original_observation(journal: Journal) -> None:
    for key in ["watch-1", "watch-2"]:
        journal.begin(command(key, "watch"))
        journal.complete(key, {"type": "response", "id": key, "result": {"session_id": "same"}})
    detached = {**command("detach", "detach"), "arguments": {"operation_id": "watch-1"}}
    journal.begin(detached)
    journal.complete(
        "detach",
        {"type": "response", "id": "detach", "result": {"detached": True, "admitted": True}},
    )
    assert [cmd["id"] for cmd, result in journal.observations()] == ["watch-2"]


@pytest.mark.asyncio
async def test_original_operation_query_is_flat_and_preserves_native_receipt_after_ack(
    journal: Journal,
) -> None:
    connector = Connector(Config("ws://localhost/", "device", "token"), journal)
    driver = FakeDriver()
    driver.finish.set()
    connector.drivers["local"] = driver
    await connector.dispatch(command())
    journal.append(
        {
            "type": "event",
            "event_id": "native-terminal",
            "kind": "completed",
            "operation_id": "original",
            "session_id": "native-session",
            "execution_id": "native-turn",
            "payload": {"status": "completed", "text": "actual result"},
        }
    )
    journal.ack("native-terminal")
    response = await connector.dispatch(
        {**command("query", "get"), "arguments": {"operation_id": "original"}}
    )
    assert response["result"]["status"] == "completed"
    assert response["result"]["session_id"] == "native-session"
    assert response["result"]["execution_id"] == "native-turn"
    assert response["result"]["pending"] is False
    assert "type" not in response["result"] and "result" not in response["result"]


def test_native_acceptance_before_reply_is_recoverable_without_dispatch_replay(
    journal: Journal,
) -> None:
    original = command()
    journal.begin(original)
    journal.append(
        {
            "type": "event",
            "event_id": "accepted-before-crash",
            "kind": "progress",
            "operation_id": "original",
            "session_id": "original-session",
            "execution_id": "original-turn",
            "payload": {"admitted": True, "status": "running"},
        }
    )
    assert journal.operation("original")["admitted"] is True
    observations = journal.observations()
    assert observations[0][1]["execution_id"] == "original-turn"
    journal.append(
        {
            "type": "event",
            "event_id": "early-terminal",
            "kind": "completed",
            "operation_id": "original",
            "payload": {"status": "completed"},
        }
    )
    journal.complete(
        "original",
        {
            "type": "response",
            "id": "original",
            "result": {
                "session_id": "original-session",
                "execution_id": "original-turn",
                "status": "running",
            },
        },
    )
    assert journal.observations() == []
    assert journal.operation("original")["status"] == "completed"


@pytest.mark.asyncio
async def test_unknown_control_is_never_admitted_or_used_to_detach(journal: Journal) -> None:
    journal.begin(command("watch-original", "watch"))
    journal.complete(
        "watch-original",
        {
            "type": "response",
            "id": "watch-original",
            "result": {
                "session_id": "native-session",
                "status": "unknown",
                "pending": True,
                "admitted": True,
            },
        },
    )

    class UnknownDriver(FakeDriver):
        async def execute(self, value: dict[str, Any]) -> dict[str, Any]:
            return {"status": "unknown", "reason": "delivery_unconfirmed"}

    connector = Connector(Config("ws://localhost/", "device", "token"), journal)
    connector.drivers["local"] = UnknownDriver()
    response = await connector.dispatch(
        {**command("detach", "detach"), "arguments": {"operation_id": "watch-original"}}
    )
    assert "admitted" not in response["result"]
    assert journal.observations()[0][0]["id"] == "watch-original"
