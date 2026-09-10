"""Run on an isolated Linux validation host, never on production conversations."""

import asyncio
import json
import tempfile
from pathlib import Path
from uuid import uuid4

from qq_ai_bot.sandbox.manager import Manager
from qq_ai_bot.workspace.store import WorkspaceStore


async def main() -> None:
    with tempfile.TemporaryDirectory(prefix="yuki-runtime-check-") as root:
        store = WorkspaceStore(Path(root) / "workspace")
        manager = Manager(
            Path(root) / "jobs",
            store,
            "yuki-python:3.8.2",
            "yuki-sandbox-internal",
            "http://172.30.251.2:3128",
        )
        await manager.recover()
        assert manager.ready, "runtime_or_network_guard_unavailable"
        worker = asyncio.create_task(manager.worker())
        cases = [
            (
                "python_and_artifact",
                (
                    "from pathlib import Path\n"
                    "import PIL,openpyxl,pypdf\n"
                    "Path('/work/outputs/result.txt').write_text('verified')\n"
                    "print('ok')"
                ),
                15,
                "succeeded",
            ),
            (
                "public_and_private",
                (
                    "import urllib.request,socket,os\n"
                    "assert urllib.request.urlopen('https://pypi.org/simple/pip/',timeout=8).status==200\n"
                    "for url in ['http://127.0.0.1','http://169.254.169.254','http://172.30.251.1']:\n"
                    " try:\n"
                    "  urllib.request.urlopen(url,timeout=2)\n"
                    "  raise AssertionError('private reachable')\n"
                    " except (OSError,ValueError): pass\n"
                    "try:\n"
                    " socket.create_connection(('172.30.251.1',22),timeout=2)\n"
                    " raise AssertionError('host reachable')\n"
                    "except OSError: pass\n"
                    "assert not os.path.exists('/var/run/docker.sock')\n"
                    "assert not os.path.exists('/app/data/qq_ai_bot.db')\n"
                    "print('network_guard_ok')"
                ),
                25,
                "succeeded",
            ),
            ("timeout", "while True: pass", 2, "failed"),
            ("memory", "a=bytearray(512*1024*1024)", 10, "failed"),
            (
                "process_limit",
                (
                    "import os,time\n"
                    "for i in range(100):\n"
                    " try: pid=os.fork()\n"
                    " except OSError: print('limited');break\n"
                    " if pid==0: time.sleep(30);os._exit(0)\n"
                    "else: raise AssertionError('unbounded process count')\n"
                ),
                15,
                "succeeded",
            ),
            ("flood", "print('x'*100000)", 10, "succeeded"),
            ("symlink", "import os\nos.symlink('/etc/passwd','/work/outputs/leak')", 10, "failed"),
        ]
        try:
            for label, code, timeout, expected in cases:
                result = await manager.handle(
                    {
                        "method": "run_python",
                        "request_id": str(uuid4()),
                        "args": {"code": code, "timeout_seconds": timeout},
                    }
                )
                async with asyncio.timeout(timeout + 40):
                    while result.get("status") not in {"succeeded", "failed", "cancelled"}:
                        await asyncio.sleep(0.2)
                        result = manager.get(result["run_id"])
                assert result["status"] == expected, (label, result)
                if label == "flood":
                    assert result["truncated"] and len(result["output"].encode()) <= 32768
                if label == "python_and_artifact":
                    assert store.read(result["artifacts"][0]["artifact_id"])["text"] == "verified"
                print(
                    json.dumps({"case": label, "status": result["status"], "passed": True}),
                    flush=True,
                )
            assert len(store.list()["items"]) == 1
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
            if manager.current:
                await manager.cleanup(manager.current)
            manager.db.close()


if __name__ == "__main__":
    asyncio.run(main())
