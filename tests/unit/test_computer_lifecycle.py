"""Yuki's own computer lifecycle at the Manager (task book §11 A1/A4/A5, R7–R9, R11).

The real headed display, session and idle sleep are tests/integration/
test_desktop_session_e2e.py; ``/etc/rc.local`` in a real environment is
scripts/sandbox_snapshot_e2e.py. These cases are the decisions the Manager makes.
"""

import asyncio
import os
import tempfile
import time
from pathlib import Path
from uuid import uuid4

import pytest
from tests.support.fake_docker import FakeDocker

from yuki.sandbox.persistent import AUTOSTART, PersistentManager
from yuki.workspace.store import WorkspaceStore

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX environment runtime")


def manager_at(tmp_path, **options):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    manager = PersistentManager(
        tmp_path / "manager",
        WorkspaceStore(tmp_path / "artifacts"),
        "base",
        "egress",
        "",
        home,
        snapshots=tmp_path / "snapshots",
        **options,
    )
    docker = FakeDocker()
    manager.command = docker.command
    manager.container_id = docker.container["Id"] + ":" + docker.container["StartedAt"]
    manager.ready = True
    return manager


def screen(manager, state="running"):
    """The desktop service run as the Manager observes it."""
    value = {"display": state, "browser": state}
    manager.desktop._service_status = lambda: dict(value)

    async def start(key):
        value.update(display="running", browser="running")
        return {}

    async def stop():
        value.update(display="stopped", browser="stopped")
        return {}

    manager.desktop._start, manager.desktop._stop = start, stop
    return value


async def test_r11_lite_has_no_screen_to_wake_or_sleep_but_snapshots(tmp_path):
    manager = manager_at(tmp_path)
    manager.desktop_manifest = {"desktop": False, "browser_mode": "none", "tools": []}
    try:
        for action in ("wake", "sleep"):
            result = await manager.desktop.action(
                "computer_action", {"action": action}, str(uuid4())
            )
            assert result["error"] == "browser_unavailable"
        manager.display_idle_minutes = 0.0001
        manager.desktop.last_activity -= 60
        await manager.desktop.idle_sleep()
        assert manager.db.execute("SELECT COUNT(*) FROM environment_mutations").fetchone()[0] == 0
        # The call observes its snapshot until the Manager worker has taken it.
        call = asyncio.create_task(
            manager.desktop.action(
                "computer_action", {"action": "snapshot", "text": "lite"}, "snap-key"
            )
        )
        async with asyncio.timeout(5):
            while manager.snapshots.find("snap-key") is None:  # noqa: ASYNC110
                await asyncio.sleep(0.01)
        await manager.snapshots.step()
        taken = await call
        assert taken["snapshot"]["note"] == "lite" and taken["snapshot"]["state"] == "ready"
    finally:
        await manager.close()


async def test_r12_wake_and_sleep_receipts_follow_the_original_key(tmp_path):
    manager = manager_at(tmp_path, testing=True)
    state = screen(manager, "stopped")
    try:
        woken = await manager.desktop.action("computer_action", {"action": "wake"}, "w")
        assert woken == {"display": "running", "browser": "running"}
        slept = await manager.desktop.action("computer_action", {"action": "sleep"}, "s")
        assert slept["display"] == "stopped" and slept["tabs"] == []
        state.update(display="running", browser="running")
        # The same key answers the original receipt; it never stops the screen again.
        assert await manager.desktop.action("computer_action", {"action": "sleep"}, "s") == slept
        assert state["display"] == "running"
        # The key is the original call: a wake key is not a sleep.
        conflict = await manager.desktop.action("computer_action", {"action": "wake"}, "s")
        assert conflict == {"error": "idempotency_conflict"}
    finally:
        await manager.close()


async def test_r7_r8_r9_idle_sleep_is_a_short_signal_never_interleaving_an_action(tmp_path):
    manager = manager_at(tmp_path, testing=True, display_idle_minutes=0.0005)
    state = screen(manager)
    try:
        # R8: a Manager counts from its own start.
        assert time.monotonic() - manager.desktop.last_activity < 5
        manager.desktop.last_activity -= 60
        # R7: an action holds the lock; the idle check waits for the next round.
        async with manager.desktop.lock:
            await manager.desktop.idle_sleep()
        assert state["display"] == "running"
        manager.desktop.streams = 1
        await manager.desktop.idle_sleep()
        assert state["display"] == "running"
        manager.desktop.streams = 0
        manager.desktop.last_activity -= 60
        manager.db.execute(
            "INSERT INTO environment_services(name,spec,enabled,run_id) VALUES "
            "('yuki-desktop','{}',1,'run-1')"
        )
        await manager.desktop.idle_sleep()
        assert state["display"] == "stopped"
        (key,) = manager.db.execute(
            "SELECT request_id FROM environment_mutations WHERE request_id LIKE 'idle:%'"
        ).fetchone()
        assert key == "idle:run-1"
        # R9: 0 never sleeps.
        state.update(display="running", browser="running")
        manager.display_idle_minutes = 0
        manager.desktop.last_activity -= 3600
        await manager.desktop.idle_sleep()
        assert state["display"] == "running"
    finally:
        await manager.close()


async def test_a1_a4_a5_autostart_runs_once_per_environment_generation(tmp_path):
    manager = manager_at(tmp_path, testing=True)
    try:
        manager.container_id = "environment:first-start"
        first = await manager.autostart()
        # A4: the Manager restarts, the environment did not: the same run, not a new one.
        assert (await manager.autostart())["run_id"] == first["run_id"]
        # A5: a new generation (OOM, restore, VM restart) runs it exactly once more.
        manager.container_id = "environment:second-start"
        second = await manager.autostart()
        assert second["run_id"] != first["run_id"]
        rows = manager.db.execute(
            "SELECT request_id FROM jobs JOIN environment_jobs USING(id) WHERE kind='service'"
        ).fetchall()
        assert sorted(row[0] for row in rows) == [
            "autostart:environment:first-start",
            "autostart:environment:second-start",
        ]
        # A1: without /etc/rc.local the run ends successfully and leaves nothing to rerun.
        assert AUTOSTART.startswith("[ -e /etc/rc.local ] || exit 0\n")
        assert AUTOSTART.rstrip().endswith("exit 0")
    finally:
        await manager.close()


async def test_actions_mark_activity_and_streams_are_counted(tmp_path):
    manager = manager_at(tmp_path, testing=True)
    screen(manager)
    try:
        manager.desktop.last_activity -= 600
        await manager.desktop.action("computer_action", {"action": "wake"}, "w")
        assert time.monotonic() - manager.desktop.last_activity < 5
        manager.desktop.last_activity -= 600

        async def stream(reader, writer):
            assert manager.desktop.streams == 1
            writer.write(b'{"ready":true}\n')
            await writer.drain()

        manager.computer_transport.display_stream = stream
        socket = Path(tempfile.mkdtemp(dir="/tmp")) / "m.sock"
        server = await asyncio.start_unix_server(manager.serve, str(socket))
        reader, writer = await asyncio.open_unix_connection(str(socket))
        writer.write(b'{"method":"display_stream","args":{}}\n')
        await writer.drain()
        assert await reader.readline() == b'{"ready":true}\n'
        writer.close()
        server.close()
        async with asyncio.timeout(5):
            while manager.desktop.streams:  # noqa: ASYNC110
                await asyncio.sleep(0.01)
        assert time.monotonic() - manager.desktop.last_activity < 5
    finally:
        await manager.close()
