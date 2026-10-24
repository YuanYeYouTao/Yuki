"""Linux display substitute; identity, API, Unix Manager and RFB filter stay real."""

import asyncio
import json
import os
import signal
import struct
import tempfile
import zlib
from pathlib import Path

from tests.support.native_computer_manager import NativeManager

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
            )
        self.manager.ready = True
        self.manager.files.root.mkdir(parents=True, exist_ok=True)
        (self.manager.files.root / "current.txt").write_text("Current workspace bytes.")
        self.manager.available_memory = lambda: 0
        self.manager.desktop.display_status = lambda: {"display": "running"}
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
        if self.native_work:
            for row in self.manager.active():
                if not row["dispatched"]:
                    self.manager.queue.put_nowait(row["id"])
            self.worker = asyncio.create_task(self.manager.worker())
            await self.container.sandbox_completions.start()
        await self.container.client_api.computer.start()

    async def close(self):
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
            return {
                "events": self.events,
                "clipboard": self.clipboard,
                "connections": self.connections,
                "mutations": self.manager.db.execute(
                    "SELECT COUNT(*) FROM environment_mutations"
                ).fetchone()[0],
                "control": self.manager.computer.control(),
            }
