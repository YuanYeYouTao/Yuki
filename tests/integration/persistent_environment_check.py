"""Opt-in real execd acceptance, run in the trusted local Docker validation image.

No QQ/LLM credentials and no outbound messages. Uses a dedicated test directory
on Docker's Linux host, shared with the validation harness at the same path.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from uuid import uuid4

from qq_ai_bot.sandbox.persistent import CONTAINER, PersistentManager
from qq_ai_bot.workspace.store import WorkspaceStore

ROOT = Path(os.environ.get("YUKI_ACCEPTANCE_ROOT", "/tmp/yuki-environment-acceptance"))


async def main():
    root = ROOT / uuid4().hex
    home = root / "home"
    home.mkdir(parents=True)
    manager = PersistentManager(
        root / "manager",
        WorkspaceStore(root / "artifacts"),
        "yuki-environment:20260912",
        "bridge",
        "",
        home,
        runtime="runc",
        testing=True,
    )
    await manager.recover()
    worker = asyncio.create_task(manager.worker())

    async def call(method, args):
        return await manager.handle({"method": method, "args": args, "request_id": str(uuid4())})

    async def completed(result, wait_seconds=40):
        assert result.get("run_id"), result
        async with asyncio.timeout(wait_seconds):
            while result.get("pending"):
                await asyncio.sleep(0.3)
                result = manager.get(result["run_id"])
        return result

    try:
        write = await call("workspace_write", {"path": "项目/文件.txt", "text": "文件工具"})
        assert write.get("version"), write
        done = await completed(
            await call(
                "terminal_exec",
                {"command": "cat '项目/文件.txt'; printf '终端修改' > '项目/文件.txt'"},
            )
        )
        assert done["status"] == "succeeded" and "文件工具" in done["output"], done
        current = await call("workspace_read", {"path": "项目/文件.txt"})
        assert current["text"] == "终端修改", current
        conflict = await call(
            "workspace_write",
            {"path": "项目/文件.txt", "text": "bad", "expected_version": write["version"]},
        )
        assert conflict["error"] == "version_conflict", conflict
        snapshot = await call("workspace_publish", {"path": "项目/文件.txt"})
        assert snapshot.get("immutable"), snapshot
        assert manager.store.read(snapshot["artifact_id"])["text"] == "终端修改"
        print("PASS shared files, content conflict, immutable publication", flush=True)

        terminal = await call(
            "terminal_exec",
            {"command": "bash --noprofile --norc", "tty": True, "timeout_seconds": 0},
        )
        assert terminal["pending"], terminal
        await call(
            "terminal_write",
            {
                "run_id": terminal["run_id"],
                "text": "export CHECK=state; cd /workspace/项目; "
                "function hello(){ echo persistent-$CHECK; }; hello\n",
            },
        )
        await asyncio.sleep(1)
        first = await call("terminal_read", {"run_id": terminal["run_id"]})
        assert "persistent-state" in first["output"], first
        await call("terminal_write", {"run_id": terminal["run_id"], "text": "pwd; hello\n"})
        await asyncio.sleep(1)
        follow = await call(
            "terminal_read", {"run_id": terminal["run_id"], "cursor": first["next_cursor"]}
        )
        assert "persistent-state" in follow["output"] and "/workspace/项目" in follow["output"], (
            follow
        )
        await call("terminal_write", {"run_id": terminal["run_id"], "text": "exit\n"})
        assert (await completed(manager.get(terminal["run_id"])))["exit_code"] == 0
        print("PASS real PTY shell state and incremental input/output", flush=True)

        task = await call(
            "terminal_exec", {"command": "echo once >> once.txt; sleep 12; echo recovered"}
        )
        assert task["pending"], task
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
        await manager.close()
        await asyncio.sleep(10)  # Job finishes while the Manager is disconnected.
        manager = PersistentManager(
            root / "manager",
            WorkspaceStore(root / "artifacts"),
            "yuki-environment:20260912",
            "bridge",
            "",
            home,
            runtime="runc",
            testing=True,
        )
        await manager.recover()
        worker = asyncio.create_task(manager.worker())
        recovered = await completed(manager.get(task["run_id"]))
        assert recovered["exit_code"] == 0 and "recovered" in recovered["output"], recovered
        assert (home / "workspace/once.txt").read_text().splitlines() == ["once"]
        print("PASS Manager reconnect after completed job, no duplicate execution", flush=True)

        long = await call("terminal_exec", {"command": "sleep 60"})
        await call("cancel_code_run", {"run_id": long["run_id"]})
        assert (await completed(manager.get(long["run_id"])))["status"] == "cancelled"
        python = await completed(
            await call(
                "run_python",
                {
                    "code": "from pathlib import Path; "
                    "Path('/work/outputs/hello.txt').write_text('compat'); print('python-ok')"
                },
            )
        )
        assert python["exit_code"] == 0 and len(python["artifacts"]) == 1, python
        print("PASS cancellation and run_python artifact compatibility", flush=True)

        await call(
            "environment_service",
            {
                "action": "register",
                "name": "http",
                "command": "python -m http.server 8765 --bind 127.0.0.1",
                "restart": "always",
            },
        )
        service = await call("environment_service", {"action": "start", "name": "http"})
        assert service.get("enabled"), service
        await asyncio.sleep(2)
        internal = await completed(
            await call("terminal_exec", {"command": "curl -sf http://127.0.0.1:8765/"})
        )
        assert internal["exit_code"] == 0, internal
        await call("environment_service", {"action": "stop", "name": "http"})
        print("PASS registered internal service and stop", flush=True)
        print(
            json.dumps({"acceptance": "passed", "root": str(root), "image": manager.image}),
            flush=True,
        )
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
        await manager.close()
        # Only the explicitly labelled acceptance environment is removed.
        await manager.command("docker", "rm", "-f", CONTAINER)


if __name__ == "__main__":
    asyncio.run(main())
