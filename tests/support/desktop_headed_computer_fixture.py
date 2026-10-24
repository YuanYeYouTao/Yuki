"""Actual Xvnc/Chromium, original Manager records and an isolated Linux process boundary."""

import asyncio
import json
import os
import shlex
import shutil
import signal
import sys
import tempfile
from pathlib import Path
from uuid import uuid4

from tests.support.native_computer_manager import NativeManager


class DesktopHeadedComputerFixture:
    native_work = True
    external = True
    headed = True

    def __init__(self, root, container):
        if os.name != "posix" or not all(
            shutil.which(name) for name in ("Xvnc", "openbox", "chromium")
        ):
            raise RuntimeError("The headed fixture requires actual Linux desktop components")
        self.container = container
        self.short = tempfile.TemporaryDirectory(prefix="yc-headed-", dir="/tmp")
        container.sandbox_client.socket = Path(self.short.name) / "m.sock"
        native = root / "headed-computer"
        native.mkdir(exist_ok=True)
        self.manager = NativeManager(native)
        self.manager.desktop_manifest = {"desktop": True, "tools": []}
        self.manager.files.root.mkdir(parents=True, exist_ok=True)
        (self.manager.files.root / "current.txt").write_text("Actual headed workspace bytes.")
        binary = self.manager.home / ".local/bin"
        binary.mkdir(parents=True, exist_ok=True)
        cli = Path(__file__).parents[2] / "src/yuki/sandbox/yuki_computer.py"
        wrapper = binary / "yuki-computer"
        wrapper.write_text("#!/bin/sh\nexec " + shlex.join([sys.executable, str(cli)]) + ' "$@"\n')
        wrapper.chmod(0o755)
        original = self.manager.command

        async def command(*args, **options):
            if args[:3] == ("docker", "exec", self.manager.name) and args[3] == "yuki-computer":
                return await original(
                    "env",
                    "HOME=" + str(self.manager.home),
                    "YUKI_COMPUTER_RUNTIME=" + str(self.manager.computer.root),
                    sys.executable,
                    str(cli),
                    *args[4:],
                )
            return await original(*args, **options)

        self.manager.command = command
        self.manager.computer_transport.display_stream = self.display_stream
        self.server = None
        self.worker = None

    async def display_stream(self, reader, writer):
        upstream_reader, upstream_writer = await asyncio.open_connection("127.0.0.1", 5901)

        async def copy(source, destination):
            while data := await source.read(65536):
                destination.write(data)
                await destination.drain()

        writer.write(b'{"ready":true}\n')
        await writer.drain()
        tasks = [
            asyncio.create_task(copy(reader, upstream_writer)),
            asyncio.create_task(copy(upstream_reader, writer)),
        ]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            upstream_writer.close()
            await upstream_writer.wait_closed()

    async def start(self, person):
        self.server = await asyncio.start_unix_server(
            self.manager.serve, str(self.container.sandbox_client.socket)
        )
        service = Path(__file__).parents[2] / "src/yuki/sandbox/desktop_service.py"
        await self.manager.service(
            {
                "action": "register",
                "name": "yuki-desktop",
                "command": shlex.join([sys.executable, str(service)]),
                "cwd": str(self.manager.home),
                "restart": "on-failure",
            }
        )
        for row in self.manager.active():
            await self.manager.reconcile(row)
        self.worker = asyncio.create_task(self.manager.worker())
        await self.manager.desktop.handle("display_wake", {}, str(uuid4()))
        async with asyncio.timeout(60):
            while self.manager.desktop.display_status()["display"] != "running":
                row = self.manager.db.execute(
                    "SELECT run_id FROM environment_services WHERE name='yuki-desktop'"
                ).fetchone()
                if row and row["run_id"]:
                    assert self.manager.get(row["run_id"])["pending"]
                await asyncio.sleep(0.1)
        await self.container.sandbox_completions.start()
        await self.container.client_api.computer.start()

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

    def attach(self, app):
        @app.get("/__test/computer")
        async def facts():
            return {
                "headed": True,
                "external_manager": False,
                "environment": self.manager.environment_status(),
                "display": self.manager.desktop.display_status(),
                **await self.work_facts(),
            }

    async def close(self):
        await self.container.client_api.computer.close()
        await self.container.sandbox_completions.close()
        if self.server:
            self.server.close()
            await self.server.wait_closed()
        if self.worker:
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)
        # Stop only process groups recorded by this fixture's Supervisor.
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
        self.short.cleanup()
