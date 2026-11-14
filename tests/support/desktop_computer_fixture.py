"""Linux display substitute; identity, API, Unix Manager and RFB filter stay real.

Snapshots, restores and idle sleep run the Manager's own code against a modelled Docker
engine (``tests.support.fake_docker``); the display is a flag the wake/sleep path flips.
Test endpoints for Desktop e2e:

- ``GET /__test/computer``: RFB events, control, ``display``, open ``display_streams``,
  ``idle`` (minutes configured and since the last use), ``snapshots``/``restores`` as the
  Manager recorded them, snapshot image tags, and ``marker`` (``workspace/marker.txt``).
- ``POST /__test/computer/marker {"text"}``: write the marker in HOME (restore check).
- ``POST /__test/computer/idle``: age the last use past the idle minutes; the next
  Manager round sleeps the screen exactly as after real idleness.
- ``POST /__test/computer/snapshot-delay {"seconds"}``: hold commits so ``creating``
  can be seen.

``YUKI_DESKTOP_IDLE_MINUTES`` (default 30) and ``YUKI_DESKTOP_SNAPSHOT_KEEP`` (default 3)
set the Manager's own product settings.
"""

import asyncio
import json
import os
import signal
import struct
import tempfile
import zlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

from tests.support.fake_docker import FakeDocker
from tests.support.native_computer_manager import NativeManager

from yuki.sandbox import persistent
from yuki.sandbox.persistent import PersistentManager
from yuki.workspace.store import WorkspaceStore


class DesktopComputerFixture:
    def __init__(self, root, container, *, native_work=False):
        self.container = container
        self.short = tempfile.TemporaryDirectory(prefix="yc-display-", dir="/tmp")
        socket = Path(self.short.name) / "m.sock"
        container.sandbox_client.socket = socket
        home = root / "computer-home"
        home.mkdir(exist_ok=True)
        self.native_work = native_work
        self.worker = None
        if native_work:
            native_root = root / "native-computer"
            native_root.mkdir(exist_ok=True)
            self.manager = NativeManager(native_root)
        else:
            self.manager = PersistentManager(
                root / "computer-manager",
                WorkspaceStore(root / "computer-artifacts"),
                "fixture-display",
                "egress",
                "",
                home,
                testing=True,
                snapshot_keep=int(os.environ.get("YUKI_DESKTOP_SNAPSHOT_KEEP", "3")),
                display_idle_minutes=float(os.environ.get("YUKI_DESKTOP_IDLE_MINUTES", "30")),
            )
        self.docker = FakeDocker()
        self.display = "running"
        self.lifecycle = None
        if native_work:
            self.manager.desktop.display_status = lambda: {"display": "running"}
        else:
            self.manager.command = self.docker.command
            self.manager.container_id = (
                self.docker.container["Id"] + ":" + self.docker.container["StartedAt"]
            )
            self.manager.set_setting("execd_token", "fixture")
            self.manager.desktop._service_status = lambda: {
                "display": self.display,
                "browser": "running" if self.display == "running" else "stopped",
            }

            # The desktop service run the Manager would have; each wake is a new run.
            self.manager.db.execute(
                "INSERT INTO environment_services(name,spec,enabled,run_id) "
                "VALUES ('yuki-desktop','{}',1,?)",
                (str(uuid4()),),
            )
            self.manager.db.commit()

            async def start(key):
                if self.display != "running":
                    with self.manager.db:
                        self.manager.db.execute(
                            "UPDATE environment_services SET run_id=? WHERE name='yuki-desktop'",
                            (str(uuid4()),),
                        )
                self.display = "running"
                return {}

            async def stop():
                self.display = "stopped"
                return {}

            self.manager.desktop._start = start
            self.manager.desktop._stop = stop
        self.manager.ready = True
        self.manager.files.root.mkdir(parents=True, exist_ok=True)
        (self.manager.files.root / "current.txt").write_text("Current workspace bytes.")
        self.manager.available_memory = lambda: 0
        runtime = self.manager.desktop.handle

        async def handle(method, args, key):
            if method == "tabs_list":
                return {
                    "available": True,
                    "tabs": [
                        {
                            "target_id": "user-tab",
                            "title": "User page",
                            "url": "https://example.test/path",
                        }
                    ],
                }
            return await runtime(method, args, key)

        self.manager.desktop.handle = handle
        self.manager.computer_transport.display_stream = self.rfb
        self.events = []
        self.clipboard = ""
        self.connections = 0
        self.writers = set()
        self.server = None

    async def start(self, person):
        self.server = await asyncio.start_unix_server(
            self.manager.serve, str(self.container.sandbox_client.socket)
        )
        if not self.native_work:
            # Restores run the Manager's real recover(); only execd is not there.
            persistent.Execd = lambda *_: SimpleNamespace(request=AsyncMock(return_value={}))
            self.lifecycle = asyncio.create_task(self.manager_rounds())
        if self.native_work:
            for row in self.manager.active():
                if not row["dispatched"]:
                    self.manager.queue.put_nowait(row["id"])
            self.worker = asyncio.create_task(self.manager.worker())
            await self.container.sandbox_completions.start()
        await self.container.client_api.computer.start()

    async def manager_rounds(self):
        """The lifecycle part of the Manager worker: snapshots, restores, idle sleep."""
        while True:
            try:
                await self.manager.snapshots.step()
                await self.manager.desktop.idle_sleep()
                self.manager.desktop.observe_display()
            except Exception as exc:  # the Manager worker logs and keeps going, too
                print(f"fixture manager round: {type(exc).__name__}: {exc}", flush=True)
            await asyncio.sleep(0.2)

    async def close(self):
        if self.lifecycle:
            self.lifecycle.cancel()
            await asyncio.gather(self.lifecycle, return_exceptions=True)
        if self.native_work:
            await self.container.sandbox_completions.close()
        await self.container.client_api.computer.close()
        if self.server:
            self.server.close()
            await self.server.wait_closed()
        for writer in self.writers:
            writer.close()
        if self.worker:
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)
            # Only process groups recorded by this fixture's real Supervisor.
            for state in self.manager.runtime_root.glob("*/state/status.json"):
                value = json.loads(state.read_text())
                if value["status"] == "running":
                    for field in ("pid", "supervisor_pid"):
                        try:
                            if field == "pid":
                                os.killpg(value[field], signal.SIGKILL)
                            else:
                                os.kill(value[field], signal.SIGKILL)
                        except ProcessLookupError:
                            pass
            for process in self.manager.processes:
                if process.returncode is None:
                    process.kill()
                await process.wait()
        await self.manager.close()
        self.short.cleanup()

    async def work_facts(self):
        return {
            "manager_jobs": [
                {"run_id": row["id"], "status": self.manager.get(row["id"])["status"]}
                for row in self.manager.db.execute(
                    "SELECT id FROM environment_jobs WHERE kind='terminal_exec'"
                )
            ],
            "waits": [dict(row) for row in self.manager.db.execute("SELECT * FROM help_requests")],
            "control": self.manager.computer.control(),
        }

    async def rfb(self, reader, writer):
        self.connections += 1
        self.writers.add(writer)
        try:
            writer.write(b'{"ready":true}\nRFB 003.008\n')
            await writer.drain()
            assert await reader.readexactly(12) == b"RFB 003.008\n"
            writer.write(b"\x01\x01")
            await writer.drain()
            assert await reader.readexactly(1) == b"\x01"
            writer.write(b"\x00\x00\x00\x00")
            await writer.drain()
            await reader.readexactly(1)
            writer.write(
                struct.pack(">HHBBBBHHHBBBxxxI", 960, 600, 32, 24, 0, 1, 255, 255, 255, 16, 8, 0, 7)
                + b"Fixture"
            )
            writer.write(b"\x03\0\0\0" + struct.pack(">iII", -8, 0x1F000001, 0))
            await writer.drain()
            first_frame = True
            while True:
                kind = (await reader.readexactly(1))[0]
                if kind == 0:
                    await reader.readexactly(19)
                elif kind == 2:
                    header = await reader.readexactly(3)
                    count = struct.unpack_from(">H", header, 1)[0]
                    encodings = list(
                        struct.unpack(f">{count}i", await reader.readexactly(count * 4))
                    )
                    self.events.append({"kind": "encodings", "values": encodings})
                    if -258 in encodings:
                        writer.write(struct.pack(">BBHHHHHi", 0, 0, 1, 0, 0, 0, 0, -258))
                        await writer.drain()
                elif kind == 3:
                    await reader.readexactly(9)
                    self.events.append({"kind": "read"})
                    if first_frame:
                        first_frame = False
                        writer.write(struct.pack(">BBHHHHHi", 0, 0, 1, 0, 0, 960, 600, 0))
                        writer.write(b"\x35\x30\x2d\0" * (960 * 600))
                        await writer.drain()
                elif kind in {4, 5, 255}:
                    data = await reader.readexactly({4: 7, 5: 5, 255: 11}[kind])
                    self.events.append(
                        {
                            "kind": {4: "key", 5: "pointer", 255: "extended"}[kind],
                            "hex": (bytes([kind]) + data).hex(),
                        }
                    )
                elif kind == 6:
                    header = await reader.readexactly(7)
                    length = struct.unpack_from(">i", header, 3)[0]
                    data = await reader.readexactly(abs(length))
                    flags = struct.unpack_from(">I", data)[0] if length < 0 else 0
                    self.events.append({"kind": "clipboard", "flags": flags})
                    if flags & 0x01000000:
                        continue
                    if flags & 0x08000000:
                        writer.write(b"\x03\0\0\0" + struct.pack(">iI", -4, 0x02000001))
                        await writer.drain()
                    elif flags & 0x10000000:
                        raw = zlib.decompressobj().decompress(data[4:])
                        self.clipboard = raw[4:].rstrip(b"\0").decode("utf-8")
                    elif length >= 0:
                        self.clipboard = data.decode("latin-1")
                elif kind == 150:
                    await reader.readexactly(9)
                elif kind == 248:
                    header = await reader.readexactly(8)
                    await reader.readexactly(header[7])
                else:
                    raise ValueError(f"unexpected_rfb_client_type:{kind}")
        except asyncio.IncompleteReadError:
            pass
        finally:
            self.writers.discard(writer)

    def attach(self, app):
        @app.get("/__test/computer")
        async def facts():
            marker = self.manager.files.root / "marker.txt"
            return {
                "events": self.events,
                "clipboard": self.clipboard,
                "connections": self.connections,
                "display_streams": self.manager.desktop.streams,
                "display": self.manager.desktop.display_status()["display"],
                "idle": self.manager.desktop.idle_status(),
                "mutations": self.manager.db.execute(
                    "SELECT COUNT(*) FROM environment_mutations"
                ).fetchone()[0],
                "idle_receipts": [
                    {"request_id": row[0], "result": json.loads(row[1])}
                    for row in self.manager.db.execute(
                        "SELECT request_id,result FROM environment_mutations "
                        "WHERE request_id LIKE 'idle:%'"
                    )
                ],
                "control": self.manager.computer.control(),
                "snapshots": [
                    dict(row)
                    for row in self.manager.db.execute("SELECT * FROM environment_snapshots")
                ],
                "restores": [
                    dict(row)
                    for row in self.manager.db.execute("SELECT * FROM environment_restores")
                ],
                "snapshot_images": self.docker.snapshot_tags(),
                "marker": marker.read_text() if marker.exists() else None,
            }

        @app.post("/__test/computer/marker")
        async def write_marker(body: dict):
            (self.manager.files.root / "marker.txt").write_text(body["text"])
            return {"marker": body["text"]}

        @app.post("/__test/computer/idle")
        async def age_last_use():
            self.manager.desktop.last_activity -= self.manager.display_idle_minutes * 60 + 1
            return self.manager.desktop.idle_status()

        @app.post("/__test/computer/snapshot-delay")
        async def snapshot_delay(body: dict):
            self.docker.delay = float(body["seconds"])
            return {"seconds": self.docker.delay}
