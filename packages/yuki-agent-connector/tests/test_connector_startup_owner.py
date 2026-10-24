from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import sys

import pytest
from websockets.asyncio.server import serve

from yuki_agent_connector.config import Config, Instance
from yuki_agent_connector.connector import Connector
from yuki_agent_connector.journal import Journal


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("code", "reason", "authorization_lost"),
    [(1008, "unsupported_connector_protocol", False), (4401, "revoked", True)],
)
async def test_protocol_rejection_is_distinct_from_revocation_and_keeps_native_owner(
    tmp_path, monkeypatch, caplog, code, reason, authorization_lost
):
    observed, native_stopped = asyncio.Event(), asyncio.Event()
    authorization = []

    class Driver:
        def __init__(self):
            self.capabilities = ["list"]

        async def stop(self):
            native_stopped.set()

    journal = Journal(tmp_path / "journal.sqlite3", namespace="server/device")
    connector = Connector(Config("ws://localhost/", "device", "fixture"), journal)
    connector.drivers["original"] = Driver()

    async def already_started():
        pass

    original_authorization_lost = connector.authorization_lost

    async def record_authorization():
        authorization.append(True)
        observed.set()
        await original_authorization_lost()

    class ProtocolNotice(logging.Handler):
        def emit(self, record):
            if "unsupported_connector_protocol" in record.getMessage():
                observed.set()

    handler = ProtocolNotice()
    logger = logging.getLogger("yuki_agent_connector.connector")
    logger.addHandler(handler)
    monkeypatch.setattr(connector, "start_drivers", already_started)
    monkeypatch.setattr(connector, "authorization_lost", record_authorization)

    async def reject(socket):
        await socket.recv()
        await socket.close(code, reason)

    try:
        async with serve(reject, "127.0.0.1", 0) as server:
            connector.config = Config(
                f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}", "device", "fixture"
            )
            running = asyncio.create_task(connector.run())
            try:
                await asyncio.wait_for(observed.wait(), 5)
                assert bool(authorization) is authorization_lost
                assert not running.done() and not native_stopped.is_set()
                if not authorization_lost:
                    assert "unsupported_connector_protocol" in caplog.text
                    assert "authorization ended" not in caplog.text
                connector.stopped.set()
                await asyncio.wait_for(running, 5)
                assert native_stopped.is_set()
            finally:
                running.cancel()
                await asyncio.gather(running, return_exceptions=True)
    finally:
        logger.removeHandler(handler)
        journal.close()


@pytest.mark.asyncio
async def test_ready_instance_connects_and_later_ready_catalog_does_not_wait_for_slow_recovery(
    tmp_path, monkeypatch
):
    start_release, recovery_release = asyncio.Event(), asyncio.Event()
    later_visible, fast_operated = asyncio.Event(), asyncio.Event()
    stopped = set()

    class Driver:
        def __init__(self, spec, emit):
            self.spec = spec
            self.capabilities = ["list", "get", "watch"]

        async def start(self):
            if self.spec.id == "later":
                await start_release.wait()

        async def recover(self, command, result):
            if self.spec.id == "recovering":
                await recovery_release.wait()

        async def execute(self, command):
            return {"instance_id": self.spec.id, "pending": False}

        async def stop(self):
            stopped.add(self.spec.id)

    journal = Journal(tmp_path / "journal.sqlite3", namespace="server/device")
    original = {"id": "original-watch", "instance_id": "recovering", "action": "watch"}
    journal.begin(original)
    journal.complete(
        "original-watch", {"result": {"pending": True, "session_id": "original-session"}}
    )
    monkeypatch.setattr("yuki_agent_connector.connector.make_driver", Driver)

    async def peer(socket):
        hello = json.loads(await socket.recv())
        assert hello["protocol_version"] == 3
        initial = {item["id"]: item["status"] for item in hello["instances"]}
        assert initial == {"fast": "available", "later": "unavailable", "recovering": "available"}
        await socket.send(
            json.dumps(
                {
                    "type": "command",
                    "id": "query-fast",
                    "instance_id": "fast",
                    "action": "list",
                    "arguments": {},
                }
            )
        )
        async for raw in socket:
            message = json.loads(raw)
            if message["type"] == "response":
                assert message["id"] == "query-fast"
                assert message["result"]["instance_id"] == "fast"
                fast_operated.set()
                start_release.set()
            if message["type"] == "catalog":
                statuses = {item["id"]: item["status"] for item in message["instances"]}
                if statuses["later"] == "available":
                    assert statuses["fast"] == "available"
                    assert statuses["recovering"] == "available"
                    later_visible.set()

    async with serve(peer, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        connector = Connector(
            Config(
                f"ws://127.0.0.1:{port}",
                "device",
                "fixture",
                instances=[
                    Instance(name, "fake", name, "fake") for name in ("fast", "later", "recovering")
                ],
            ),
            journal,
        )
        running = asyncio.create_task(connector.run())
        try:
            await asyncio.wait_for(fast_operated.wait(), 5)
            await asyncio.wait_for(later_visible.wait(), 5)
            assert not recovery_release.is_set()
        finally:
            running.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await running
    assert stopped == {"fast", "later", "recovering"}
    assert journal.observations()[0][0]["id"] == "original-watch"
    journal.close()


@pytest.mark.asyncio
async def test_repeated_service_cancel_joins_initializing_driver_stop_before_return(
    tmp_path, monkeypatch
):
    started = {name: asyncio.Event() for name in ("ready", "initializing")}
    cleaning, release = asyncio.Event(), asyncio.Event()
    processes, stopped = {}, set()

    class Driver:
        def __init__(self, spec, emit):
            self.spec = spec
            self.capabilities = ["get"]

        async def start(self):
            processes[self.spec.id] = await asyncio.create_subprocess_exec(
                sys.executable, "-c", "import time; time.sleep(60)"
            )
            started[self.spec.id].set()
            if self.spec.id == "initializing":
                await asyncio.Event().wait()

        async def recover(self, command, result):
            pass

        async def stop(self):
            cleaning.set()
            await release.wait()
            process = processes[self.spec.id]
            if process.returncode is None:
                process.terminate()
            await process.wait()
            stopped.add(self.spec.id)

    monkeypatch.setattr("yuki_agent_connector.connector.make_driver", Driver)
    journal = Journal(tmp_path / "journal.sqlite3", namespace="server/device")
    connector = Connector(
        Config(
            "ws://127.0.0.1:9",
            "device",
            "fixture",
            instances=[Instance(name, "fake", name, "fake") for name in started],
        ),
        journal,
    )
    running = asyncio.create_task(connector.run())
    try:
        await asyncio.wait_for(asyncio.gather(*(event.wait() for event in started.values())), 5)
        running.cancel("first-service-stop")
        await asyncio.wait_for(cleaning.wait(), 5)
        running.cancel("repeated-service-stop")
        await asyncio.sleep(0)
        assert not running.done()
        assert not stopped
        release.set()
        with pytest.raises(asyncio.CancelledError, match="first-service-stop"):
            await running
        assert stopped == set(started)
        assert all(process.returncode is not None for process in processes.values())
    finally:
        release.set()
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)
        for process in processes.values():
            if process.returncode is None:
                process.terminate()
            await process.wait()
        journal.close()
