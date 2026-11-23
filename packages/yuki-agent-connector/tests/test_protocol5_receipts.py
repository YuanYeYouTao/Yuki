"""Device protocol 5: agent mutations answer with retransmitted receipt events.

Failure cases written before the implementation (task book §4.10, §10.2):
- a mutating agent command produces a ``response`` frame instead of a receipt event;
- a refusal (driver refusal, invalid command) produces no receipt event;
- a receipt lost before its ACK is not retransmitted after reconnecting;
- a duplicate command ID executes the native command again;
- reads and device resources lose their RPC response;
- respond/steer/cancel/queue aimed at an observe watch reach the driver (F1).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve

from yuki_agent_connector import PROTOCOL_VERSION, __version__
from yuki_agent_connector import connector as connector_module
from yuki_agent_connector.config import Config, Instance
from yuki_agent_connector.connector import Connector
from yuki_agent_connector.drivers import DriverError
from yuki_agent_connector.journal import Journal


def command(identity: str = "original", action: str = "prompt", **arguments: Any) -> dict[str, Any]:
    return {
        "type": "command",
        "id": identity,
        "instance_id": "local",
        "action": action,
        "arguments": arguments or {"prompt": "original input"},
    }


class Driver:
    def __init__(self, refusal: DriverError | None = None) -> None:
        self.spec = Instance("local", "fake", "Fake", "fake")
        self.capabilities = ["list", "get", "prompt", "watch", "respond", "steer", "cancel"]
        self.calls: list[dict[str, Any]] = []
        self.refusal = refusal
        self.started = asyncio.Event()
        self.finish = asyncio.Event()
        self.finish.set()

    async def execute(self, value: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(value)
        self.started.set()
        await self.finish.wait()
        if self.refusal is not None:
            raise self.refusal
        return {
            "session_id": "native-session",
            "execution_id": "native-turn",
            "status": "running",
            "pending": True,
            "admitted": True,
        }

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def recover(self, value: dict[str, Any], result: dict[str, Any]) -> None:
        pass


@pytest.fixture
def journal(tmp_path: Path) -> Any:
    value = Journal(tmp_path / "private" / "journal.db", namespace="server/device")
    yield value
    value.close()


def events(journal: Journal) -> list[dict[str, Any]]:
    return [json.loads(row[0]) for row in journal.db.execute("SELECT event FROM events")]


def receipts(journal: Journal, key: str) -> list[dict[str, Any]]:
    return [
        item
        for item in events(journal)
        if item["kind"] == "receipt" and item["operation_id"] == key
    ]


def test_hello_declares_protocol_five_and_connector_0_9_0(journal: Journal) -> None:
    assert __version__ == "0.9.8"
    assert PROTOCOL_VERSION == 5
    hello = Connector(Config("ws://localhost/", "device", "token"), journal).hello()
    assert hello["protocol_version"] == 5


@pytest.mark.asyncio
async def test_mutation_is_answered_by_a_receipt_event_never_a_response_frame(
    journal: Journal,
) -> None:
    connector = Connector(Config("ws://localhost/", "device", "token"), journal)
    driver = Driver()
    connector.drivers["local"] = driver
    frames: list[dict[str, Any]] = []
    done = asyncio.Event()

    async def peer(socket: Any) -> None:
        await socket.recv()  # hello
        await socket.send(json.dumps(command()))
        while True:
            frame = json.loads(await socket.recv())
            frames.append(frame)
            if frame.get("kind") == "receipt":
                break
        # A read afterwards still gets its RPC response; nothing answered the prompt.
        await socket.send(json.dumps(command("read", "get", operation_id="original")))
        frames.append(json.loads(await socket.recv()))
        await socket.send(json.dumps({"type": "ack", "event_id": frames[-2]["event_id"]}))
        done.set()
        await socket.wait_closed()

    async with serve(peer, "127.0.0.1", 0) as server:
        async with connect(f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}") as socket:
            running = asyncio.create_task(connector.connection(socket))
            await asyncio.wait_for(done.wait(), 3)
            await socket.close()
            await running
    receipt, read = frames[-2], frames[-1]
    assert not any(f.get("type") == "response" and f.get("id") == "original" for f in frames)
    assert receipt["type"] == "event" and receipt["kind"] == "receipt"
    assert receipt["instance_id"] == "local" and receipt["operation_id"] == "original"
    assert isinstance(receipt["event_id"], str) and receipt["event_id"]
    projection = journal.operation("original")
    assert receipt["payload"] == {"result": projection}
    assert projection["execution_id"] == "native-turn" and projection["admitted"] is True
    assert receipt["sequence"] == projection["state_sequence"]
    assert read == {"type": "response", "id": "read", "result": projection}
    assert len(driver.calls) == 1


@pytest.mark.asyncio
async def test_native_refusal_is_a_receipt_with_the_original_error_shape(
    journal: Journal,
) -> None:
    connector = Connector(Config("ws://localhost/", "device", "token"), journal)
    connector.drivers["local"] = Driver(DriverError("conflict", "busy", {"hint": "queue"}))
    assert await connector.dispatch(command()) is None
    [receipt] = receipts(journal, "original")
    result = receipt["payload"]["result"]
    assert result["status"] == "failed" and result["admitted"] is False
    assert result["error"]["code"] == "conflict"
    assert result["error"]["details"] == {"hint": "queue"}
    assert result == journal.operation("original")


@pytest.mark.asyncio
async def test_reused_id_with_other_parameters_is_refused_as_receipt_without_touching_original(
    journal: Journal,
) -> None:
    connector = Connector(Config("ws://localhost/", "device", "token"), journal)
    driver = Driver()
    connector.drivers["local"] = driver
    await connector.dispatch(command())
    before = journal.operation("original")
    for item in receipts(journal, "original"):
        journal.ack(item["event_id"])
    assert await connector.dispatch(command(prompt="changed")) is None
    [refusal] = receipts(journal, "original")
    assert refusal["payload"]["result"]["error"]["code"] == "invalid_command"
    assert refusal["payload"]["result"]["admitted"] is False
    assert journal.operation("original") == before
    assert len(driver.calls) == 1


@pytest.mark.asyncio
async def test_duplicate_command_never_executes_again_and_its_receipt_is_sent_again(
    journal: Journal,
) -> None:
    connector = Connector(Config("ws://localhost/", "device", "token"), journal)
    driver = Driver()
    driver.finish.clear()
    connector.drivers["local"] = driver
    first = asyncio.create_task(connector.dispatch(command()))
    await driver.started.wait()
    # Still dispatching: the original's own receipt will follow; no unknown receipt now.
    assert await connector.dispatch(command()) is None
    assert receipts(journal, "original") == []
    driver.finish.set()
    assert await first is None
    [original] = receipts(journal, "original")
    journal.ack(original["event_id"])
    assert await connector.dispatch(command()) is None
    [again] = receipts(journal, "original")
    assert again["event_id"] != original["event_id"]
    assert again["payload"]["result"]["execution_id"] == "native-turn"
    assert again["payload"]["result"] == journal.operation("original")
    assert len(driver.calls) == 1


@pytest.mark.asyncio
async def test_receipt_lost_before_ack_is_retransmitted_after_reconnect(journal: Journal) -> None:
    connector = Connector(Config("ws://localhost/", "device", "token"), journal)
    connector.drivers["local"] = Driver()
    seen: list[dict[str, Any]] = []
    connections = 0
    acked = asyncio.Event()

    async def peer(socket: Any) -> None:
        nonlocal connections
        connections += 1
        await socket.recv()  # hello
        if connections == 1:
            await socket.send(json.dumps(command()))
            seen.append(json.loads(await socket.recv()))
            await socket.close()  # The receipt crossed the wire; its ACK never did.
        else:
            seen.append(json.loads(await socket.recv()))
            await socket.send(json.dumps({"type": "ack", "event_id": seen[-1]["event_id"]}))
            acked.set()
            await socket.wait_closed()

    async with serve(peer, "127.0.0.1", 0) as server:
        url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"
        async with connect(url) as socket:
            await connector.connection(socket)
        async with connect(url) as socket:
            running = asyncio.create_task(connector.connection(socket))
            await asyncio.wait_for(acked.wait(), 3)
            await socket.close()
            await running
    assert [item["kind"] for item in seen] == ["receipt", "receipt"]
    assert seen[0]["event_id"] == seen[1]["event_id"]
    assert journal.next_event() is None


@pytest.mark.asyncio
async def test_reads_and_device_resources_keep_their_rpc_response(journal: Journal) -> None:
    connector = Connector(Config("ws://localhost/", "device", "token"), journal)
    connector.drivers["local"] = Driver()
    listed = await connector.dispatch(command("list", "list"))
    assert listed is not None and listed["type"] == "response" and listed["id"] == "list"
    status = await connector.dispatch(
        {"type": "command", "id": "status", "resource": "terminal", "action": "status"}
    )
    assert status is not None and status["type"] == "response" and "result" in status
    assert events(journal) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["respond", "steer", "cancel", "queue"])
async def test_controls_aimed_at_an_observe_watch_are_refused_before_any_driver(
    journal: Journal, action: str
) -> None:
    driver = Driver()
    watch = command("observer", "watch", observation="observe")
    journal.begin(watch, owner_source=connector_module.instance_source(driver.spec))
    journal.complete(
        "observer",
        {"type": "response", "id": "observer", "result": {"session_id": "s", "pending": True}},
    )
    connector = Connector(Config("ws://localhost/", "device", "token"), journal)
    connector.drivers["local"] = driver
    connector.operations["observer"] = driver
    control = {
        **command(f"{action}-it", action, operation_id="observer", prompt="x", option_id="deny"),
        "session_id": "s",
    }
    assert await connector.dispatch(control) is None
    [refusal] = receipts(journal, f"{action}-it")
    assert refusal["payload"]["result"]["error"]["code"] == "observe_only"
    assert driver.calls == []


@pytest.mark.asyncio
async def test_queue_naming_a_session_of_a_finished_operation_reaches_the_instance(
    journal: Journal,
) -> None:
    driver = Driver()
    driver.spec = Instance("local", "claude", "Claude", "claude")
    journal.begin(command("finished"), owner_source=None)
    journal.complete(
        "finished",
        {"type": "response", "id": "finished", "result": {"session_id": "native-session"}},
    )
    connector = Connector(Config("ws://localhost/", "device", "token", [driver.spec]), journal)
    connector.drivers["local"] = driver
    queued = {
        **command("queued", "queue", operation_id="finished", prompt="later"),
        "session_id": "native-session",
    }
    assert await connector.dispatch(queued) is None
    [receipt] = receipts(journal, "queued")
    assert "error" not in receipt["payload"]["result"]
    assert driver.calls and driver.calls[0]["id"] == "queued"
