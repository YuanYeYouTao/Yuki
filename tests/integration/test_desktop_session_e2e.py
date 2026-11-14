"""Her desktop session, idle sleep and Yuki's own wake/sleep, on a real headed display.

Task book: docs/architecture/Yuki自己的电脑任务书-2026-11-14.md §11 (A6, A8–A12,
R1–R5, R7, R9, R12, R14 at the Manager; R14's device refusal is the Bot's and is in
tests/unit/test_computer_lifecycle.py). The Manager, Supervisor, desktop service,
Xvnc, openbox-session, Chromium and the driver are the real programs of the headed
environment image (CI ``desktop-headed``); only the container boundary is absent.

Artifact: ``test-results/desktop-session/`` — ``steps.json`` (every request, receipt,
status and process list) and one PNG per step from the real screen.
"""

import asyncio
import base64
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import pytest
from tests.support.native_computer_manager import NativeManager

ROOT = Path(__file__).resolve().parents[2]
ARTIFACT = ROOT / "test-results" / "desktop-session"
SCREEN = ("Xvnc", "openbox", "chromium", "xmessage")

pytestmark = pytest.mark.skipif(
    not all(shutil.which(name) for name in ("Xvnc", "openbox-session", "chromium", "xmessage")),
    reason="Requires the actual headed environment packages",
)


def processes() -> dict[str, list[int]]:
    found: dict[str, list[int]] = {}
    # Without docker-init as PID 1 nothing reaps orphans here: zombies are not running.
    for line in subprocess.run(
        ["ps", "-eo", "pid=,stat=,comm="], capture_output=True, text=True, check=True
    ).stdout.splitlines():
        pid, state, name = line.split(None, 2)
        if name in SCREEN and not state.startswith("Z"):
            found.setdefault(name, []).append(int(pid))
    return found


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    stat = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    return stat[0] != "Z"


class Session:
    def __init__(self, root: Path) -> None:
        self.manager = NativeManager(root)
        self.manager.desktop_manifest = {"desktop": True, "browser_mode": "headed", "tools": []}
        self.home = self.manager.home
        self.steps: list[dict] = []
        self.worker: asyncio.Task | None = None
        self.server: asyncio.base_events.Server | None = None
        self.socket = Path(f"/tmp/yds-{uuid4().hex[:8]}.sock")
        self.manager.computer_transport.display_stream = self.display_stream

    async def display_stream(self, reader, writer):
        # The container's socat bridge, as the headed fixture: loopback RFB on 5901.
        upstream_reader, upstream_writer = await asyncio.open_connection("127.0.0.1", 5901)
        writer.write(b'{"ready":true}\n')
        await writer.drain()

        async def copy(source, destination):
            while data := await source.read(65536):
                destination.write(data)
                await destination.drain()

        tasks = [
            asyncio.create_task(copy(reader, upstream_writer)),
            asyncio.create_task(copy(upstream_reader, writer)),
        ]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            upstream_writer.close()

    async def start(self) -> None:
        service = ROOT / "src/yuki/sandbox/desktop_service.py"
        driver = ROOT / "packages/yuki-agent-connector/src"
        await self.manager.service(
            {
                "action": "register",
                "name": "yuki-desktop",
                "command": "PYTHONPATH="
                + shlex.quote(str(driver))
                + " "
                + shlex.join([sys.executable, str(service)]),
                "cwd": str(self.home),
                "restart": "on-failure",
            }
        )
        self.server = await asyncio.start_unix_server(self.manager.serve, str(self.socket))
        self.worker = asyncio.create_task(self.manager.worker())

    async def close(self) -> None:
        if self.worker:
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)
        if self.server:
            self.server.close()
        for path in self.manager.runtime_root.glob("*/state/status.json"):
            value = json.loads(path.read_text())
            if value["status"] == "running":
                for field in ("pid", "supervisor_pid"):
                    try:
                        if field == "pid":
                            os.killpg(value[field], signal.SIGTERM)
                        else:
                            os.kill(value[field], signal.SIGTERM)
                    except ProcessLookupError:
                        pass
        for process in self.manager.processes:
            await asyncio.wait_for(process.wait(), 15)
        await self.manager.close()
        self.socket.unlink(missing_ok=True)

    async def computer(self, action: str, key: str | None = None, **args) -> dict:
        key = key or str(uuid4())
        # The Bot passes the deployment's vision size with every screen action.
        size = {"max_dimension": 1280, "max_pixels": 1_000_000} if action == "screenshot" else {}
        return await self.manager.desktop.action(
            "computer_action", {"action": action, **size, **args}, key
        )

    async def until(self, check, seconds: float = 60, message: str = "") -> None:
        deadline = time.monotonic() + seconds
        while not check():
            assert time.monotonic() < deadline, message() if callable(message) else message
            await asyncio.sleep(0.2)

    def status(self) -> dict:
        return self.manager.desktop.idle_status()

    async def record(self, name: str, *, screenshot: bool = True, **facts) -> None:
        entry = {"step": name, "status": self.status(), "processes": processes(), **facts}
        if screenshot and self.manager.desktop.browser_status() == "running":
            shot = await self.computer("screenshot")
            image = shot.pop("image")
            path = ARTIFACT / f"{len(self.steps):02d}-{name}.png"
            path.write_bytes(base64.b64decode(image["base64"]))
            entry["screenshot"] = path.name
        self.steps.append(entry)
        (ARTIFACT / "steps.json").write_text(
            json.dumps(self.steps, ensure_ascii=False, indent=2, default=str)
        )

    async def terminal(self, command: str) -> dict:
        run = await self.manager.submit(
            "terminal_exec", {"command": command, "cwd": str(self.home)}, str(uuid4())
        )
        return await self.manager.wait_result(run["run_id"], wait_seconds=30)


async def test_desktop_session_idle_sleep_and_yuki_wake_sleep(tmp_path):
    shutil.rmtree(ARTIFACT, ignore_errors=True)
    ARTIFACT.mkdir(parents=True)
    (tmp_path / "manager").mkdir()
    session = Session(tmp_path / "manager")
    home = session.home
    autostart = home / ".config/openbox/autostart"
    autostart.parent.mkdir(parents=True)
    autostart.write_text(
        'echo "$(date +%s.%N)" >> "$HOME/autostart-ran"\n'
        "xmessage -geometry +80+80 'autostart marker' &\n"
    )
    manager = session.manager
    await session.start()
    try:
        # A6: no ~/.xsession: openbox-session runs her autostart file.
        woken = await session.computer("wake", "wake-1")
        assert woken["display"] == "running" and woken["browser"] == "running", woken
        await session.until(lambda: (home / "autostart-ran").exists(), message="no autostart")
        await session.until(lambda: "xmessage" in processes(), message="no autostart window")
        assert session.status()["session"] == "default"
        assert "openbox" in processes()
        await session.record("a6_default_session_autostart")

        # R12: the same key answers the original receipt; a sleep keeps the open pages.
        assert await session.computer("wake", "wake-1") == woken
        opened = await manager.desktop.action(
            "browser_action", {"action": "open", "url": "data:text/html,<h1>tab</h1>"}, "open-1"
        )
        assert not opened.get("error"), opened
        # A GUI program started from the terminal and a plain background process.
        background = await session.terminal(
            "nohup sleep 3600 >/dev/null 2>&1 & echo $! > bg.pid; "
            "DISPLAY=:1 nohup xmessage -geometry +300+300 terminal-window >/dev/null 2>&1 & "
            "echo $! > gui.pid"
        )
        assert background["status"] == "succeeded", background
        await session.until(lambda: len(processes().get("xmessage", [])) == 2)
        slept = await session.computer("sleep", "sleep-1")
        assert slept["display"] == "stopped" and any(
            "tab" in json.dumps(tab) for tab in slept["tabs"]
        ), slept
        assert await session.computer("sleep", "sleep-1") == slept
        await session.until(lambda: not processes(), message=f"screen left: {processes()}")
        # R4/R5: the terminal's background process stays; X clients ended with the display.
        assert alive(int((home / "bg.pid").read_text()))
        assert not alive(int((home / "gui.pid").read_text()))
        again = await session.computer("sleep", "sleep-2")
        assert again["display"] == "stopped" and again["tabs"] == []
        await session.record("r12_sleep_receipts_r4_r5", screenshot=False, sleep=slept)

        # A8: ~/.xsession fails at once; the display and Yuki's browser stay.
        (home / ".xsession").write_text("echo custom-session-output\nexit 3\n")
        await session.computer("wake")
        await session.until(lambda: session.status()["session"] == "exited:3")
        assert "custom-session-output" in (home / ".xsession-errors").read_text()
        assert {"Xvnc", "chromium"} <= set(processes()) and "openbox" not in processes()
        assert manager.desktop.browser_status() == "running"
        await session.record("a8_failed_xsession_keeps_display")

        # A9: her own session replaces Openbox; killing her window manager ends only it.
        (home / ".xsession").write_text("exec openbox\n")
        await session.computer("sleep")
        await session.until(lambda: not processes())
        await session.computer("wake")
        await session.until(lambda: "openbox" in processes())
        assert session.status()["session"] == "running"
        xvnc = processes()["Xvnc"]
        os.kill(processes()["openbox"][0], signal.SIGTERM)
        await session.until(lambda: (session.status()["session"] or "").startswith("exited:"))
        await asyncio.sleep(3)
        assert "openbox" not in processes() and processes()["Xvnc"] == xvnc
        assert manager.desktop.browser_status() == "running"
        await session.record("a9_her_window_manager_is_hers")

        # A12: closing Chromium reopens only Chromium and its driver.
        (home / ".xsession").unlink()
        await session.computer("sleep")
        await session.until(lambda: not processes())
        await session.computer("wake")
        await session.until(lambda: "xmessage" in processes())
        before = processes()
        for pid in before["chromium"]:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        await session.until(
            lambda: set(processes().get("chromium", [])) - set(before["chromium"]),
            message="Chromium did not reopen",
        )
        await session.until(lambda: manager.desktop.browser_status() == "running")
        after = processes()
        assert after["Xvnc"] == before["Xvnc"] and after["xmessage"] == before["xmessage"]
        await session.record("a12_closed_browser_reopens_alone")

        # A10: Xvnc ends: the whole service ends and restarts; her session runs again.
        runs = (home / "autostart-ran").read_text().count("\n")
        os.kill(processes()["Xvnc"][0], signal.SIGKILL)
        await session.until(
            lambda: (home / "autostart-ran").read_text().count("\n") > runs,
            seconds=90,
            message="the session did not run again",
        )
        await session.until(lambda: manager.desktop.browser_status() == "running")
        await session.record("a10_display_restart_runs_session_again")

        # R9: 0 never sleeps.
        assert manager.display_idle_minutes == 0
        manager.desktop.last_activity -= 3600
        await asyncio.sleep(2)
        assert manager.desktop.browser_status() == "running"

        # R2: an open display stream keeps it awake; closing it restarts the count.
        manager.display_idle_minutes = 0.05  # 3 seconds, a product setting
        manager.desktop.touch()
        reader, writer = await asyncio.open_unix_connection(str(session.socket))
        writer.write(b'{"method":"display_stream","args":{}}\n')
        await writer.drain()
        assert json.loads(await reader.readline()) == {"ready": True}
        assert (await reader.readexactly(12)).startswith(b"RFB")
        assert session.status()["display_streams"] == 1
        await asyncio.sleep(6)
        assert manager.desktop.browser_status() == "running"
        await session.record("r2_stream_keeps_awake", stream_open=True)
        writer.close()
        await session.until(lambda: session.status()["display_streams"] == 0)
        closed_at = time.monotonic()

        # R3: the person holding control keeps it awake.
        taken = manager.computer.set(
            str(uuid4()),
            {
                "action": "take",
                "actor": {
                    "person_id": str(uuid4()),
                    "device_id": str(uuid4()),
                    "device_label": "desk",
                },
            },
        )
        await asyncio.sleep(5)
        assert manager.desktop.browser_status() == "running"
        await session.record("r3_human_control_keeps_awake")
        # S11 for sleep: Yuki does not sleep the screen the person holds.
        held = await session.computer("sleep")
        assert held["error"] == "control_held"
        manager.computer.set(
            str(uuid4()),
            {
                "action": "return",
                "lease_id": taken["control"]["lease_id"],
                "actor": taken["control"] | {"system": False},
            },
        )
        # R1: idle again, the screen layer sleeps with an idle receipt.
        run_id = manager.db.execute(
            "SELECT run_id FROM environment_services WHERE name='yuki-desktop'"
        ).fetchone()[0]
        await session.until(
            lambda: manager.desktop.browser_status() != "running",
            seconds=30,
            message=lambda: json.dumps(
                {
                    "status": session.status(),
                    "control": manager.computer.control(),
                    "locked": manager.desktop.lock.locked(),
                    "worker": repr(session.worker),
                },
                default=str,
            ),
        )
        slept_after = time.monotonic() - closed_at
        await session.until(lambda: not processes(), message=f"screen left: {processes()}")
        receipt = json.loads(
            manager.db.execute(
                "SELECT result FROM environment_mutations WHERE request_id=?", (f"idle:{run_id}",)
            ).fetchone()[0]
        )
        assert receipt["display"] == "stopped" and receipt["idle_minutes"] == 0.05
        assert alive(int((home / "bg.pid").read_text()))
        await session.record(
            "r1_idle_sleep", screenshot=False, receipt=receipt, seconds=round(slept_after, 1)
        )

        # The next use wakes it again (R6's automatic wake through _ready()).
        shot = await session.computer("screenshot")
        assert shot.get("image"), shot
        await session.record("woken_by_next_use")
    finally:
        await session.close()


async def test_headless_never_runs_her_xsession(tmp_path):
    # A11: the headless profile has no X; ~/.xsession is not run and nothing fails.
    (tmp_path / "manager").mkdir()
    session = Session(tmp_path / "manager")
    session.manager.desktop_manifest = {"desktop": False, "browser_mode": "headless", "tools": []}
    (session.manager.computer.root).mkdir(parents=True, exist_ok=True)
    (session.manager.computer.root / "browser-mode").write_text("headless")
    (session.home / ".xsession").write_text("touch $HOME/xsession-ran\n")
    await session.start()
    try:
        woken = await session.computer("wake")
        assert woken["browser"] == "running" and woken["display"] == "unavailable", woken
        assert not (session.home / "xsession-ran").exists()
        assert session.status()["session"] is None
        slept = await session.computer("sleep")
        assert slept["browser"] == "stopped", slept
    finally:
        await session.close()
