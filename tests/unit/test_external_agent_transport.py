"""Transport connections do not own or restart native executions."""

import asyncio

import pytest

from yuki.external_agents.transport import DeviceConnection, DeviceConnections, DeviceOffline


class Socket:
    def __init__(self, *, failure=None):
        self.sent = asyncio.Queue()
        self.messages = []
        self.closed = []
        self.failure = failure

    async def send_json(self, value):
        self.messages.append(value)
        self.sent.put_nowait(value)
        if self.failure is not None:
            raise self.failure

    async def close(self, code=1000, reason=None):
        self.closed.append((code, reason))


async def test_same_original_operation_waiters_share_one_delivery():
    socket = Socket()
    connection = DeviceConnection("computer", socket)
    command = {"id": "original-operation", "action": "steer"}
    first = asyncio.create_task(connection.request(command))
    await socket.sent.get()
    second = asyncio.create_task(connection.request(command))
    await asyncio.sleep(0)
    result = {"status": "accepted", "execution_id": "original-turn"}
    connection.respond({"id": command["id"], "result": result})
    assert await first == await second == result
    assert socket.messages == [command]
    assert connection.pending == {}


async def test_disconnection_keeps_dispatch_unknown_without_native_cancel_or_resend():
    socket = Socket()
    connection = DeviceConnection("computer", socket)
    command = {"id": "original-operation", "action": "prompt"}
    request = asyncio.create_task(connection.request(command))
    await socket.sent.get()
    connection.disconnected()
    with pytest.raises(DeviceOffline, match="device_disconnected"):
        await request
    connection.respond({"id": command["id"], "result": {"status": "completed"}})
    assert socket.messages == [command]
    assert connection.pending == {}


async def test_replacing_transport_does_not_detach_new_connection_or_restart_old_request():
    connections = DeviceConnections()
    old_socket, new_socket = Socket(), Socket()
    old = DeviceConnection("computer", old_socket)
    new = DeviceConnection("computer", new_socket)
    await connections.attach(old)
    waiting = asyncio.create_task(old.request({"id": "old-operation", "action": "prompt"}))
    await old_socket.sent.get()
    await connections.attach(new)
    with pytest.raises(DeviceOffline):
        await waiting
    assert old_socket.closed == [(1000, "connection_replaced")]
    connections.detach(old)
    assert connections.get("computer") is new
    assert new_socket.messages == []
    queried = asyncio.create_task(new.request({"id": "query", "action": "get"}))
    await new_socket.sent.get()
    new.respond({"id": "query", "result": {"status": "running"}})
    assert await queried == {"status": "running"}
    await connections.close()
    assert new_socket.messages == [{"id": "query", "action": "get"}]
    assert new_socket.closed == [(1001, None)]
    assert not connections.online("computer")


async def test_same_request_id_on_different_devices_is_not_one_rpc():
    first_socket, second_socket = Socket(), Socket()
    first, second = (
        DeviceConnection("first", first_socket),
        DeviceConnection("second", second_socket),
    )
    one = asyncio.create_task(first.request({"id": "same", "action": "get"}))
    two = asyncio.create_task(second.request({"id": "same", "action": "get"}))
    await first_socket.sent.get()
    await second_socket.sent.get()
    first.respond({"id": "same", "result": {"device": "first"}})
    second.respond({"id": "same", "result": {"device": "second"}})
    assert await one == {"device": "first"}
    assert await two == {"device": "second"}


async def test_failed_send_is_not_retried_and_cancelled_wait_does_not_cancel_native():
    failed_socket = Socket(failure=OSError("write confirmation lost"))
    failed = DeviceConnection("computer", failed_socket)
    with pytest.raises(OSError, match="write confirmation lost"):
        await failed.request({"id": "original", "action": "prompt"})
    assert failed_socket.messages == [{"id": "original", "action": "prompt"}]
    assert failed.pending == {}
    socket = Socket()
    connection = DeviceConnection("computer", socket)
    waiting = asyncio.create_task(connection.request({"id": "read", "action": "get"}))
    await socket.sent.get()
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    connection.respond({"id": "read", "result": {"status": "running"}})
    assert socket.messages == [{"id": "read", "action": "get"}]
    assert connection.pending == {}
