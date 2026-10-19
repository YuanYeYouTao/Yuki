"""Real HTTP/WebSocket and Unix Manager; RFB endpoint substitutes the Linux desktop."""

import asyncio
import socket
import struct
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
import uvicorn
from fastapi import FastAPI
from tests.unit.test_computer_commands import admin as admin_fixture
from tests.unit.test_computer_commands import computer as computer_fixture
from tests.unit.test_computer_commands import personal as personal_fixture
from tests.unit.test_computer_http import desktop_http as desktop_http_fixture
from tests.unit.test_computer_http import until
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

admin = admin_fixture
computer = computer_fixture
personal = personal_fixture
desktop_http = desktop_http_fixture


@pytest.fixture
async def wire(desktop_http, monkeypatch):
    env = desktop_http
    received = asyncio.Queue()

    async def rfb(reader, writer):
        writer.write(b'{"ready":true}\nRFB 003.008\n')
        await writer.drain()
        assert await reader.readexactly(12) == b"RFB 003.008\n"
        writer.write(b"\x01\x01")
        await writer.drain()
        assert await reader.readexactly(1) == b"\x01"
        writer.write(b"\x00\x00\x00\x00")
        await writer.drain()
        assert await reader.readexactly(1) == b"\x01"
        # 32-bit little endian true color framebuffer, no auth, shared client.
        writer.write(
            struct.pack(">HHBBBBHHHBBBxxxI", 16, 16, 32, 24, 0, 1, 255, 255, 255, 16, 8, 0, 4)
            + b"test"
        )
        await writer.drain()
        while data := await reader.read(65536):
            await received.put(data)

    monkeypatch.setattr(env.base.manager.computer_transport, "display_stream", rfb)
    root = FastAPI()
    root.mount("/role/yuki", env.app)
    ready = asyncio.Event()

    class Server(uvicorn.Server):
        async def startup(self, sockets=None):
            await super().startup(sockets)
            ready.set()

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    server = Server(uvicorn.Config(root, lifespan="off", access_log=False, log_level="critical"))
    task = asyncio.create_task(server.serve(sockets=[listener]))
    await asyncio.wait_for(ready.wait(), 5)
    try:
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}/role/yuki", trust_env=False
        ) as client:
            yield SimpleNamespace(env=env, client=client, received=received, port=port)
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 5)
        listener.close()


async def ticket(wire, device):
    response = await wire.client.post(
        "/client/v1/computer/display-tickets",
        json={},
        headers={"Authorization": "Bearer " + device["token"]},
    )
    assert response.status_code == 200, response.text
    value = response.json()["data"]
    assert value["ws_path"] == "/role/yuki/client/v1/computer/display"
    return f"ws://127.0.0.1:{wire.port}" + value["ws_path"] + "?ticket=" + value["ticket"]


async def handshake(ws):
    assert await ws.recv() == b"RFB 003.008\n"
    await ws.send(b"RFB 003.008\n")
    assert await ws.recv() == b"\x01\x01"
    await ws.send(b"\x01")
    assert await ws.recv() == b"\x00\x00\x00\x00"
    await ws.send(b"\x01")
    assert (await ws.recv())[24:] == b"test"


async def bytes_from(wire, size):
    value = b""
    async with asyncio.timeout(5):
        while len(value) < size:
            value += await wire.received.get()
    assert len(value) == size
    return value


async def test_prefix_one_use_binary_tickets_viewer_input_and_immediate_release(wire):
    env = wire.env
    actor, device = await env.base.paired()
    uri = await ticket(wire, device)
    # Rejecting an unsupported subprotocol does not consume the valid ticket.
    with pytest.raises(InvalidStatus):
        async with connect(uri, proxy=None):
            pass
    async with connect(uri, proxy=None, subprotocols=["binary"]) as ws:
        assert ws.subprotocol == "binary"
        await handshake(ws)
        marker = struct.pack(">BBHHHH", 3, 0, 0, 0, 16, 16)
        down = struct.pack(">BBHI", 4, 1, 0, 65)
        pointer = struct.pack(">BBHH", 5, 1, 7, 8)
        extended = struct.pack(">BBHII", 255, 0, 1, 66, 48)
        await ws.send(down + pointer + extended + marker)
        assert await bytes_from(wire, len(marker)) == marker
        taken = await env.base.commands.execute(actor, str(uuid4()), "take", {})
        lease = taken["control"]["lease_id"]
        await until(env.service, lambda: env.service.snapshot["control"].get("lease_id") == lease)
        await ws.send(down + pointer + extended + marker)
        assert await bytes_from(wire, 26 + len(marker)) == down + pointer + extended + marker
        await env.base.commands.execute(actor, str(uuid4()), "return", {"lease_id": lease})
        released = await bytes_from(wire, 26)
        assert released == (
            struct.pack(">BBHI", 4, 0, 0, 65)
            + struct.pack(">BBHII", 255, 0, 0, 66, 48)
            + struct.pack(">BBHH", 5, 0, 7, 8)
        )
        with pytest.raises(InvalidStatus):
            async with connect(uri, proxy=None, subprotocols=["binary"]):
                pass


async def test_actual_revoke_closes_connected_stream_and_releases_lease(wire):
    env = wire.env
    actor, device = await env.base.paired()
    await env.base.commands.execute(actor, str(uuid4()), "take", {})
    async with connect(await ticket(wire, device), proxy=None, subprotocols=["binary"]) as ws:
        await handshake(ws)
        await env.base.identity.revoke(actor, actor.device_id, str(uuid4()))
        with pytest.raises(ConnectionClosed):
            async with asyncio.timeout(5):
                await ws.recv()
    await until(env.service, lambda: env.base.manager.computer.control()["holder"] == "none")


async def test_expired_ticket_is_rejected_then_new_ticket_reconnects_same_lease(wire, monkeypatch):
    import time

    from yuki.client_api import computer as module

    clock = SimpleNamespace(value=time.monotonic())
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: clock.value))
    actor, device = await wire.env.base.paired()
    lease = (await wire.env.base.commands.execute(actor, str(uuid4()), "take", {}))["control"][
        "lease_id"
    ]
    assert not (await wire.env.service.get(actor))["control"]["device_online"]
    expired = await ticket(wire, device)
    clock.value += 61
    with pytest.raises(InvalidStatus):
        async with connect(expired, proxy=None, subprotocols=["binary"]):
            pass
    async with connect(await ticket(wire, device), proxy=None, subprotocols=["binary"]) as ws:
        await handshake(ws)
        assert wire.env.base.manager.computer.control()["lease_id"] == lease
        assert (await wire.env.service.get(actor))["control"]["device_online"]
    assert wire.env.base.manager.computer.control()["lease_id"] == lease


async def test_manager_watch_disconnect_closes_display_without_returning_human_control(
    wire, monkeypatch
):
    env = wire.env
    actor, device = await env.base.paired()
    lease = (await env.base.commands.execute(actor, str(uuid4()), "take", {}))["control"][
        "lease_id"
    ]
    await env.service.close()
    original = env.base.manager.computer.watch
    writers = []

    async def capture(reader, writer):
        writers.append(writer)
        await original(reader, writer)

    monkeypatch.setattr(env.base.manager.computer, "watch", capture)
    await env.service.start()
    await until(env.service, lambda: env.service.connected)
    async with connect(await ticket(wire, device), proxy=None, subprotocols=["binary"]) as ws:
        await handshake(ws)
        writers[0].close()
        with pytest.raises(ConnectionClosed):
            async with asyncio.timeout(5):
                await ws.recv()
    assert env.base.manager.computer.control()["lease_id"] == lease
