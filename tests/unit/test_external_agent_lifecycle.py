"""Connector transport shutdown joins its original database producers."""

import asyncio
import sqlite3

import pytest
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession
from tests.unit.test_external_agent_repository import native as native_fixture
from tests.unit.test_external_agent_repository import prepare
from tests.unit.test_external_agent_transport import Socket

from yuki.external_agents.transport import DeviceConnection, DeviceOffline
from yuki.persistence.database import write_failure_phase, write_failure_uncertain

native = native_fixture


async def test_socket_close_failure_still_disconnects_every_device_and_joins_retiring(native):
    original, _ = await prepare(native)

    class ClosedSocket(Socket):
        async def close(self, code=1000, reason=None):
            await super().close(code, reason)
            # Starlette raises this when a concurrent transport close already
            # sent the final ASGI message; its route cleanup can still be pending.
            raise RuntimeError('Cannot call "send" once a close message has been sent.')

    first, second = ClosedSocket(), Socket()
    first_connection = DeviceConnection("first", first)
    second_connection = DeviceConnection("second", second)
    await native.service.connections.attach(first_connection)
    await native.service.connections.attach(second_connection)
    request = asyncio.create_task(first_connection.request({"id": "original", "action": "get"}))

    async def retire():
        try:
            await second_connection.request({"id": "retiring", "action": "get"})
        except DeviceOffline:
            await native.repository.response(original.id, {"pending": False, "status": "failed"})

    retiring = asyncio.create_task(retire())
    native.service._retiring[original.id] = retiring
    await first.sent.get()
    await second.sent.get()
    try:
        with pytest.raises((RuntimeError, ExceptionGroup)):
            await native.service.close()
        assert retiring.done()
        assert second.closed == [(1001, None)]
        with pytest.raises(DeviceOffline):
            await request
        current = await native.repository.operation(original.id, native.actor)
        assert current.result_json is not None and '"failed"' in current.result_json
    finally:
        for task in (request, retiring):
            if not task.done():
                task.cancel()
        await asyncio.gather(request, retiring, return_exceptions=True)


async def test_shutdown_reports_original_unknown_commit_from_retiring_producer(native, monkeypatch):
    original, _ = await prepare(native)
    socket = Socket()
    connection = DeviceConnection("computer", socket)
    await native.service.connections.attach(connection)
    commit = AsyncSession.commit

    async def lose_ack(session):
        await commit(session)
        raise OperationalError("COMMIT", {}, sqlite3.OperationalError("original ACK lost"))

    async def retire():
        try:
            await connection.request({"id": "retiring-original", "action": "get"})
        except DeviceOffline:
            await native.repository.response(original.id, {"pending": False, "status": "failed"})

    task = asyncio.create_task(retire())
    native.service._retiring[original.id] = task
    await socket.sent.get()
    with monkeypatch.context() as scoped:
        scoped.setattr(AsyncSession, "commit", lose_ack)
        with pytest.raises(BaseExceptionGroup) as raised:
            await native.service.close()
    error = raised.value.exceptions[0]
    assert isinstance(error, OperationalError)
    assert write_failure_phase(error) == "commit" and write_failure_uncertain(error)
    assert task.done()
    current = await native.repository.operation(original.id, native.actor)
    assert current.result_json is not None and '"failed"' in current.result_json
