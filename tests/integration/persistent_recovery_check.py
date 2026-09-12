"""Opt-in package/checkpoint and failure recovery acceptance. No Bot or QQ access."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from uuid import uuid4

from qq_ai_bot.sandbox.persistent import PersistentManager
from qq_ai_bot.workspace.store import WorkspaceStore


async def main():
    root = (
        Path(os.environ.get("YUKI_ACCEPTANCE_ROOT", "/tmp/yuki-environment-acceptance"))
        / uuid4().hex
    )
    home = root / "home"
    home.mkdir(parents=True)
    options = dict(runtime="runc", testing=True, name="yuki-environment-recovery-check")
    manager = PersistentManager(
        root / "manager",
        WorkspaceStore(root / "artifacts"),
        "yuki-environment:20260912",
        "bridge",
        "",
        home,
        **options,
    )
    await manager.recover()
    worker = asyncio.create_task(manager.worker())

    async def call(method, args):
        return await manager.handle({"method": method, "args": args, "request_id": str(uuid4())})

    async def completed(result, wait_seconds=180):
        assert result.get("run_id"), result
        async with asyncio.timeout(wait_seconds):
            while result.get("pending"):
                await asyncio.sleep(0.5)
                result = manager.get(result["run_id"])
        return result

    async def reconnect(*, recreate=False):
        nonlocal manager, worker
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
        await manager.close()
        if recreate:
            await manager.command("docker", "rm", "-f", manager.name)
        manager = PersistentManager(
            root / "manager",
            WorkspaceStore(root / "artifacts"),
            "yuki-environment:20260912",
            "bridge",
            "",
            home,
            **options,
        )
        await manager.recover()
        worker = asyncio.create_task(manager.worker())

    try:
        installed = await completed(
            await call(
                "terminal_exec",
                {
                    "command": "python -m pip install --user --disable-pip-version-check "
                    "humanize==4.15.0 && python -c 'import humanize; "
                    "print(humanize.naturalsize(1024))'"
                },
            )
        )
        assert installed["exit_code"] == 0, installed
        apt = await completed(
            await call("environment_packages", {"action": "install", "packages": ["hello"]})
        )
        assert apt["exit_code"] == 0 and apt.get("checkpoint"), apt
        print("PASS pip dependency and apt installation with checkpoint", flush=True)
        await reconnect(recreate=True)
        recovered = await completed(
            await call(
                "terminal_exec",
                {
                    "command": "hello && python -c 'import humanize; "
                    "print(humanize.naturalsize(1024))'"
                },
            )
        )
        assert recovered["exit_code"] == 0, recovered
        print("PASS image checkpoint rebuild keeps apt and home dependencies", flush=True)

        await call(
            "environment_service",
            {
                "action": "register",
                "name": "clock",
                "command": "echo service-start >> service.txt; "
                "python -m http.server 8765 --bind 127.0.0.1",
                "restart": "always",
            },
        )
        service = await call("environment_service", {"action": "start", "name": "clock"})
        old_run = service["run"]["run_id"]
        task = await call("terminal_exec", {"command": "echo once >> interrupted.txt; sleep 120"})
        await manager.command("docker", "restart", "-t", "1", manager.name)
        await reconnect()
        assert manager.get(task["run_id"])["status"] == "failed"
        async with asyncio.timeout(30):
            while True:
                state = await call("environment_service", {"action": "status", "name": "clock"})
                if state["run"]["run_id"] != old_run and state["run"]["status"] == "running":
                    break
                await asyncio.sleep(1)
        assert (home / "workspace/interrupted.txt").read_text().splitlines() == ["once"]
        check = await completed(
            await call("terminal_exec", {"command": "curl -sf http://127.0.0.1:8765/"})
        )
        assert check["exit_code"] == 0, check
        await call("environment_service", {"action": "stop", "name": "clock"})
        print(
            "PASS reboot restores service and interrupts ordinary task without replay",
            flush=True,
        )

        network = await completed(
            await call(
                "terminal_exec",
                {"command": "curl --noproxy '*' --connect-timeout 2 -fsS http://127.0.0.1:1"},
            )
        )
        assert network["status"] == "failed" and network["exit_code"] != 0, network
        code, _ = await manager.command(
            "docker", "update", "--memory", "128m", "--memory-swap", "128m", manager.name
        )
        assert code == 0
        oom = await completed(
            await call(
                "terminal_exec",
                {"command": "python -c 'x=bytearray(400*1024*1024); print(len(x))'"},
            )
        )
        assert oom["status"] == "failed", oom
        assert len((home / "workspace/interrupted.txt").read_text().splitlines()) == 1
        await manager.command(
            "docker", "update", "--memory", "512m", "--memory-swap", "640m", manager.name
        )
        print("PASS explicit network failure and isolated cgroup OOM", flush=True)

        # Cancel during real package index/install work; repair must remain usable.
        installing = await call("environment_packages", {"action": "install", "packages": ["sl"]})
        if installing.get("pending"):
            await call("cancel_code_run", {"run_id": installing["run_id"]})
            stopped = await completed(manager.get(installing["run_id"]))
            assert stopped["status"] == "cancelled", stopped
        repaired = await completed(await call("environment_packages", {"action": "repair"}))
        assert repaired["exit_code"] == 0 and repaired.get("checkpoint"), repaired
        print("PASS interrupted apt operation and repair checkpoint", flush=True)
        pending = manager.completions.pending(20, 0)
        ids = [item["run_id"] for item in pending["events"]]
        assert len(ids) == len(set(ids))
        for event in pending["events"]:
            assert manager.get(event["run_id"]) == event["result"]
        print("PASS unique durable completion receipts", flush=True)
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
        await manager.close()
        await manager.command("docker", "rm", "-f", manager.name)


if __name__ == "__main__":
    asyncio.run(main())
