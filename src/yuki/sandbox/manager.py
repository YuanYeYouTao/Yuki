"""Trusted Linux host daemon. Not mounted into, or executed by, the Bot container.

Only this process controls Docker. Host roots, images and resource options are
operator configuration; requests select code or files within configured roots.
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


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--socket", type=Path, required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--persistent-home", type=Path, required=True)
    parser.add_argument("--network", default="yuki-sandbox-egress")
    parser.add_argument("--proxy", default="")
    parser.add_argument("--resolv-conf", type=Path)
    parser.add_argument(
        "--media-cache", type=Path, default=os.environ.get("YUKI_MANAGER_MEDIA_CACHE") or None
    )
    args = parser.parse_args()
    from yuki.sandbox.persistent import PersistentManager

    manager = PersistentManager(
        args.root,
        WorkspaceStore(args.workspace),
        args.image,
        args.network,
        args.proxy,
        args.persistent_home,
        resolv_conf=args.resolv_conf,
        media_cache=args.media_cache,
    )
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
