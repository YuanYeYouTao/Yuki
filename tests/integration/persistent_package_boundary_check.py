"""Check that writable home profiles/PATH never replace privileged package code."""

import asyncio
from pathlib import Path
from uuid import uuid4

from qq_ai_bot.sandbox.persistent import PersistentManager
from qq_ai_bot.workspace.store import WorkspaceStore


async def main():
    root = Path("/tmp/yuki-environment-acceptance") / uuid4().hex
    home = root / "home"
    binary = home / ".local/bin"
    binary.mkdir(parents=True)
    for name in ("python", "apt-get", "dpkg-query"):
        path = binary / name
        path.write_text("#!/bin/sh\necho shadowed > /workspace/unsafe-root\nexit 7\n")
        path.chmod(0o755)
    (home / ".bash_profile").write_text(
        'if [ "$(id -u)" = 0 ]; then echo profile > /workspace/unsafe-root; fi\n'
    )
    manager = PersistentManager(
        root / "manager",
        WorkspaceStore(root / "artifacts"),
        "yuki-environment:20260912",
        "bridge",
        "",
        home,
        testing=True,
        runtime="runc",
        name="yuki-environment-package-check",
    )
    await manager.recover()
    worker = asyncio.create_task(manager.worker())
    try:
        for method, args in (
            ("terminal_exec", {"command": "echo ordinary"}),
            ("environment_packages", {"action": "repair"}),
        ):
            result = await manager.handle(
                {"method": method, "args": args, "request_id": str(uuid4())}
            )
            async with asyncio.timeout(60):
                while result.get("pending"):
                    await asyncio.sleep(0.5)
                    result = manager.get(result["run_id"])
            assert result.get("exit_code") == 0, result
        assert not (home / "workspace/unsafe-root").exists()
        print(
            "PASS absolute supervisor, isolated root interpreter, clean apt PATH and shell",
            flush=True,
        )
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
        await manager.close()
        await manager.command("docker", "rm", "-f", manager.name)


if __name__ == "__main__":
    asyncio.run(main())
