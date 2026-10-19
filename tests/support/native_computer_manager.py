"""Native test transport for the real Manager and Supervisor, without Docker.

Only container/execd launch and inspection are replaced. CLI processes, durable
jobs, file requests, leases, completion receipts and the Manager worker are real.
"""

import asyncio
import json
import os
import signal
import sys
from pathlib import Path

from yuki.sandbox.persistent import PersistentManager
from yuki.workspace.store import WorkspaceStore

SUPERVISOR = Path(__file__).parents[2] / "src/yuki/sandbox/environment_supervisor.py"
WRAPPER = """
import sys,importlib.util
from pathlib import Path
spec=importlib.util.spec_from_file_location('native_supervisor',sys.argv[3])
s=importlib.util.module_from_spec(spec);spec.loader.exec_module(s)
root,identity=Path(sys.argv[1]),sys.argv[2]
s.Path=lambda value:root if value=='/var/lib/yuki-runtime' else Path(value)
sys.argv=['supervisor',identity]
raise SystemExit(s.main())
"""


class NativeManager(PersistentManager):
    def __init__(self, root):
        home = root / "home"
        home.mkdir(exist_ok=True)
        super().__init__(
            root / "manager",
            WorkspaceStore(root / "artifacts"),
            "native-fixture",
            "network",
            "",
            home,
            testing=True,
        )
        self.ready, self.container_id = True, "native-test:original"
        self.last_layer_check = 0
        self.processes = []
        original = self.computer.deliver_results

        def deliver():
            marker = root / "crash-after-return"
            completed = self.db.execute(
                "SELECT 1 FROM help_requests WHERE result IS NOT NULL LIMIT 1"
            ).fetchone()
            if marker.exists() and self.computer.control()["holder"] == "none" and completed:
                marker.unlink()
                os._exit(73)
            original()

        self.computer.deliver_results = deliver

    @staticmethod
    def available_memory():
        return 0

    async def inspect_container(self, *, size=False):
        return {"Id": "native-test", "State": {"Running": True, "StartedAt": "original"}}

    async def launch(self, identity):
        row = self.db.execute("SELECT * FROM jobs WHERE id=?", (identity,)).fetchone()
        args = json.loads(row["payload"])
        self.prepare_spec(identity, args, "terminal_exec")
        with self.db:
            self.db.execute(
                "UPDATE environment_jobs SET dispatched=1,container_id=? WHERE id=?",
                (self.container_id, identity),
            )
            self.db.execute("UPDATE jobs SET state='running' WHERE id=?", (identity,))
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            WRAPPER,
            str(self.runtime_root),
            identity,
            str(SUPERVISOR),
            env={**os.environ, "YUKI_COMPUTER_RUNTIME": str(self.computer.root)},
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        self.processes.append(process)


async def serve(root, socket):
    manager = NativeManager(root)
    for row in manager.active():
        if not row["dispatched"]:
            manager.queue.put_nowait(row["id"])
    server = await asyncio.start_unix_server(manager.serve, socket)
    worker = asyncio.create_task(manager.worker())
    stopped = asyncio.Event()
    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, stopped.set)
    marker = root / ".manager-ready"
    marker.write_text(json.dumps({"pid": os.getpid(), "runtime_root": str(manager.runtime_root)}))
    marker.replace(root / "manager-ready")
    try:
        await stopped.wait()
    finally:
        server.close()
        await server.wait_closed()
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
        await manager.close()


if __name__ == "__main__":
    asyncio.run(serve(Path(sys.argv[1]), sys.argv[2]))
