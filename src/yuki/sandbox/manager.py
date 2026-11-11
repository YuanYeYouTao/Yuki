"""The sandbox Manager: a Compose service beside the Bot, from the same image.

Only this process controls Docker (through the mounted socket) and the host firewall
(on the host network). The Bot reaches it through a narrow Unix socket. Requests select
code or files inside the configured environment; profiles, images and resources are
operator configuration from the deployment's ``.env``.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import stat
from pathlib import Path
from uuid import UUID

from yuki.workspace.store import WorkspaceStore

TERMINAL = {"succeeded", "failed", "cancelled"}


def identifier(raw: object) -> str:
    result = str(UUID(str(raw)))
    if result != raw:
        raise ValueError("invalid_identifier")
    return result


# Initial values for each profile; any single value can be set in the deployment's .env.
# They are deployment resources, not admission thresholds.
PROFILES: dict[str, dict[str, str]] = {
    "lite": {
        "browser_mode": "none",
        "memory_mib": "512",
        "swap_mib": "128",
        "cpus": "1",
        "pids": "256",
        "tmp_mib": "128",
        "shm_mib": "64",
    },
    "headless": {
        "browser_mode": "headless",
        "memory_mib": "1024",
        "swap_mib": "256",
        "cpus": "1.5",
        "pids": "1024",
        "tmp_mib": "256",
        "shm_mib": "128",
    },
    "desktop": {
        "browser_mode": "headed",
        "memory_mib": "3072",
        "swap_mib": "512",
        "cpus": "2",
        "pids": "2048",
        "tmp_mib": "512",
        "shm_mib": "256",
    },
}
RESOURCES = ("memory_mib", "swap_mib", "cpus", "pids", "tmp_mib", "shm_mib")


def configured(name: str, default: str = "") -> str:
    """Compose passes unset .env values as empty strings."""
    return os.environ.get("YUKI_SANDBOX_" + name.upper()) or default


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path("/var/lib/yuki-sandbox"))
    parser.add_argument("--workspace", type=Path, default=Path("/app/workspace"))
    parser.add_argument("--socket", type=Path, default=Path("/run/yuki-sandbox/manager.sock"))
    parser.add_argument("--code", type=Path, default=Path("/var/lib/yuki-sandbox-code"))
    parser.add_argument(
        "--media-cache", type=Path, default=Path("/app/data/conversation-media-cache")
    )
    args = parser.parse_args()
    profile = configured("profile", "lite")
    if profile not in PROFILES:
        raise ValueError("unknown_sandbox_profile")
    preset = PROFILES[profile]
    browser_mode = configured("browser_mode", preset["browser_mode"])
    image = configured("image") or (
        os.environ["YUKI_ENVIRONMENT_IMAGE"] + ("" if browser_mode == "none" else "-desktop")
    )
    # Files the Manager creates are shared with the Bot through its group.
    os.umask(0o007)
    from yuki.sandbox.persistent import PersistentManager

    manager = PersistentManager(
        args.root,
        WorkspaceStore(args.workspace),
        image,
        "yuki-sandbox-egress",
        configured("proxy"),
        args.root / "home",
        runtime=configured("runtime", "runc"),
        resolv_conf=configured("resolv_conf") or None,
        media_cache=args.media_cache,
        code=args.code,
        project=os.environ.get("YUKI_SANDBOX_PROJECT", ""),
        profile=profile,
        browser_mode=browser_mode,
        packages=tuple(configured("packages").split()),
    )
    for option in RESOURCES:
        manager.set_setting("resource_" + option, configured(option, preset[option]))
    try:
        await manager.recover()
        args.socket.parent.mkdir(parents=True, exist_ok=True)
        if args.socket.exists():
            if not stat.S_ISSOCK(args.socket.lstat().st_mode):
                raise ValueError("unsafe_socket")
            args.socket.unlink()
        serve_unix = getattr(asyncio, "start_unix_server", None)
        if serve_unix is None:
            raise RuntimeError("unix_socket_required")
        server = await serve_unix(manager.serve, str(args.socket), limit=262144)
        worker = asyncio.create_task(manager.worker())
        try:
            try:
                args.socket.chmod(0o660)
                task = asyncio.current_task()
                assert task is not None
                worker.add_done_callback(
                    lambda completed: task.cancel() if not completed.cancelled() else None
                )
                asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, task.cancel)
                await server.serve_forever()
            finally:
                server.close()
                await server.wait_closed()
        except asyncio.CancelledError:
            pass
        finally:
            failure = worker.exception() if worker.done() and not worker.cancelled() else None
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
            if failure is not None:
                raise RuntimeError("sandbox_worker_failed") from failure
    finally:
        await manager.close()


if __name__ == "__main__":
    asyncio.run(main())
