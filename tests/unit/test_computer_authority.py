"""Real Person/device revocation returns the original Manager lease once."""

import asyncio
import os
import tempfile
from pathlib import Path
from uuid import uuid4

import pytest
from tests.unit.test_admin_api import admin as admin_fixture
from tests.unit.test_app_messages import app_env as app_fixture
from tests.unit.test_client_works import owner
from tests.unit.test_computer_control import manager_at

from yuki.character.repository import CharacterRepository
from yuki.client_api.computer import ClientComputer
from yuki.sandbox.client import SandboxClient
from yuki.sandbox.computer_keys import release_key

admin = admin_fixture
app_env = app_fixture
pytestmark = pytest.mark.skipif(os.name != "posix", reason="Manager owns a Linux Unix socket")


async def changed(service, condition):
    hints = service.identity.database.commit_hints
    queue = hints.subscribe()
    try:
        async with asyncio.timeout(10):
            while not condition():
                await queue.get()
    finally:
        hints.unsubscribe(queue)


@pytest.mark.parametrize("revocation", ["person", "device"])
@pytest.mark.parametrize("observation", ["live", "cold", "overflow"])
async def test_real_revocation_returns_same_release_key_once_and_keeps_watch_alive(
    app_env, database, tmp_path, revocation, observation, monkeypatch
):
    device, person = await owner(app_env, database)
    principal = await app_env.identity.authenticate(device["token"])
    character = CharacterRepository(app_env.identity, app_env.harness.settings)
    await character.initialize("Computer authority test")
    manager = manager_at(tmp_path)
    short = tempfile.TemporaryDirectory(prefix="yc-auth-", dir="/tmp")
    socket = Path(short.name) / "manager.sock"
    server = await asyncio.start_unix_server(manager.serve, str(socket))
    service = ClientComputer(app_env.identity, SandboxClient(socket))
    returns = []
    set_control = manager.computer.set

    def observe(key, arguments):
        if arguments["action"] == "return":
            returns.append((key, arguments))
        return set_control(key, arguments)

    manager.computer.set = observe
    try:
        if observation != "cold":
            await service.start()
            await changed(service, lambda: service.connected)
        # Cold recovery starts with a real existing lease and no Desktop observation.
        result = await service.commands.execute(
            principal, str(uuid4()), "take", {"note": "actual lease"}
        )
        lease = result["control"]["lease_id"]
        if observation != "cold":
            await changed(
                service,
                lambda: (service.snapshot or {}).get("control", {}).get("lease_id") == lease,
            )
        hints = database.commit_hints
        if observation == "overflow":
            # First observe the real authorized lease. This also waits until the
            # authority watcher has finished its prior reads and waits for hints.
            observed = asyncio.Event()
            authority_changed = service._authority_changed

            async def observed_authority():
                await authority_changed()
                if asyncio.current_task() is service._tasks[1]:
                    observed.set()

            monkeypatch.setattr(service, "_authority_changed", observed_authority)
            hints.publish()
            await asyncio.wait_for(observed.wait(), 10)

        async def revoke():
            if revocation == "person":
                from yuki.identity.db_models import CanonicalPersonModel

                async with database.immediate_session() as session:
                    original = await session.get(CanonicalPersonModel, person)
                    original.enabled = False
            else:
                await app_env.identity.revoke(principal, principal.device_id, str(uuid4()))

        if observation == "overflow":
            # Lose only this domain action's disposable notifications; its real
            # identity/role SQL and durable receipts still commit normally.
            with monkeypatch.context() as patch:
                patch.setattr(hints, "publish", lambda: None)
                await revoke()
            assert manager.computer.control()["holder"] == "human"
            queue = next(iter(hints.subscribers))
            for _ in range(queue.maxsize + 1):
                hints.publish()
            await asyncio.wait_for(service._tasks[1], 10)
            assert not service._streams
        else:
            await revoke()
        if observation == "cold":
            assert not service._tasks and not service._streams
            await service.start()
        await changed(
            service, lambda: (service.snapshot or {}).get("control", {}).get("holder") == "none"
        )
        await service._authority_changed()
        key = release_key(lease)
        assert returns == [
            (
                key,
                {
                    "action": "return",
                    "lease_id": lease,
                    "actor": {"system": True},
                    "reason": "person_disabled" if revocation == "person" else "device_revoked",
                },
            )
        ]
        original = manager.computer.request_get(key)
        assert original["control"] == {"holder": "none"}
        assert original["by"] == "system"
        if observation == "overflow":
            assert not service._tasks[0].done() and service._tasks[1].done()
        else:
            assert all(not task.done() for task in service._tasks)
    finally:
        await service.close()
        server.close()
        await server.wait_closed()
        await manager.close()
        short.cleanup()
