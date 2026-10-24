"""Unsupported protocol is not a retry loop; lost authority hints close input."""

import asyncio
from contextlib import asynccontextmanager

import pytest
from starlette.websockets import WebSocketState
from tests.support.client_identity import client_identity_service

from yuki.client_api.computer import ClientComputer


class UnavailableSandbox:
    def __init__(self, error):
        self.error = error
        self.calls = 0

    @asynccontextmanager
    async def stream(self, _method, _arguments):
        self.calls += 1
        raise self.error
        yield  # pragma: no cover


@pytest.mark.parametrize("error", [ValueError("unknown_method"), NotImplementedError()])
async def test_unsupported_observer_stops_and_only_explicit_start_tries_again(database, error):
    sandbox = UnavailableSandbox(error)
    computer = ClientComputer(client_identity_service(database), sandbox)
    try:
        await computer.start()
        await asyncio.wait_for(computer._tasks[0], 1)
        assert sandbox.calls == 1
        await computer.start()
        await asyncio.wait_for(computer._tasks[0], 1)
        assert sandbox.calls == 2
    finally:
        await computer.close()


class ConnectedSandbox:
    def __init__(self):
        self.started = asyncio.Queue()
        self.calls = 0

    @asynccontextmanager
    async def stream(self, _method, _arguments):
        self.calls += 1
        self.started.put_nowait(self.calls)
        yield {"control": {"holder": "none"}}, asyncio.StreamReader(), None


class DisplaySocket:
    application_state = WebSocketState.CONNECTED

    async def close(self, **_arguments):
        self.application_state = WebSocketState.DISCONNECTED


async def test_authority_overflow_closes_display_and_explicit_start_restores_observation(database):
    sandbox = ConnectedSandbox()
    computer = ClientComputer(client_identity_service(database), sandbox)
    socket = DisplaySocket()

    async def current_authority():
        pass  # Actual owner/device revocation is covered by test_computer_authority.

    computer._authority_changed = current_authority
    computer._streams[socket] = None
    try:
        await computer.start()
        assert await sandbox.started.get() == 1
        # Publish without yielding: exercise the real bounded CommitHints overflow.
        for _ in range(65):
            database.commit_hints.publish()
        await asyncio.wait_for(computer._tasks[1], 1)
        assert socket.application_state is WebSocketState.DISCONNECTED
        await computer.start()
        assert await sandbox.started.get() == 2
        assert not any(task.done() for task in computer._tasks)
    finally:
        await computer.close()


class ObservedSandbox:
    def __init__(self):
        self.active = set()
        self.owners = set()
        self.peak = 0
        self.started = asyncio.Queue()

    @asynccontextmanager
    async def stream(self, _method, _arguments):
        task = asyncio.current_task()
        self.active.add(task)
        self.owners.add(task)
        self.peak = max(self.peak, len(self.active))
        self.started.put_nowait(task)
        try:
            yield {"control": {"holder": "none"}}, asyncio.StreamReader(), None
        finally:
            self.active.discard(task)


async def test_concurrent_restart_and_close_leave_one_observer_then_no_orphan_subscription(
    database,
):
    sandbox = ObservedSandbox()
    hints = database.commit_hints
    previous = set(hints.subscribers)
    computer = ClientComputer(client_identity_service(database), sandbox)
    try:
        await computer.start()
        await sandbox.started.get()
        queues = hints.subscribers - previous
        assert len(queues) == 1
        for _ in range(max(queue.maxsize for queue in queues) + 1):
            hints.publish()
        await computer._tasks[1]
        await asyncio.gather(computer.start(), computer.start())
        await sandbox.started.get()
        assert sandbox.peak == 1 and len(sandbox.active) == 1
        assert len(hints.subscribers - previous) == 1
        await asyncio.gather(computer.start(), computer.close(), computer.start(), computer.close())
    finally:
        await computer.close()
    assert not sandbox.active
    assert all(task.done() for task in sandbox.owners)
    assert hints.subscribers == previous


async def test_restart_surfaces_unexpected_completed_observer_error(database):
    sandbox = UnavailableSandbox(RuntimeError("actual unexpected observer failure"))
    computer = ClientComputer(client_identity_service(database), sandbox)
    try:
        await computer.start()
        with pytest.raises(RuntimeError, match="actual unexpected observer failure"):
            await computer._tasks[0]
        with pytest.raises(RuntimeError, match="actual unexpected observer failure"):
            await computer.start()
    finally:
        await computer.close()
