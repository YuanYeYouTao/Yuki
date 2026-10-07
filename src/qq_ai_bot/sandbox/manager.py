"""Trusted Linux host daemon. Not mounted into, or executed by, the Bot container.

Only this process controls Docker. All paths, images and resource options are
operator configuration; untrusted requests contain code and artifact IDs only.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import signal
import stat
from pathlib import Path
from uuid import UUID

from qq_ai_bot.sandbox.client import MAX_CONTROL_UPLOAD_WIRE
from qq_ai_bot.workspace.store import WorkspaceStore

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
    parser.add_argument("--migrate-only", action="store_true")
    parser.add_argument("--network", default="yuki-sandbox-internal")
    parser.add_argument("--proxy", default="http://172.30.251.2:3128")
    args = parser.parse_args()
    from qq_ai_bot.sandbox.persistent import PersistentManager

    manager = PersistentManager(
        args.root,
        WorkspaceStore(args.workspace),
        args.image,
        args.network,
        args.proxy,
        args.persistent_home,
    )
    if args.migrate_only:
        print(json.dumps(manager.migrate_files()))
        return
    await manager.recover()
    args.socket.parent.mkdir(parents=True, exist_ok=True)
    if args.socket.exists():
        if not stat.S_ISSOCK(args.socket.lstat().st_mode):
            raise ValueError("unsafe_socket")
        args.socket.unlink()
    serve_unix = getattr(asyncio, "start_unix_server", None)
    if serve_unix is None:
        raise RuntimeError("unix_socket_required")
    server = await serve_unix(manager.serve, str(args.socket), limit=MAX_CONTROL_UPLOAD_WIRE)
    args.socket.chmod(0o660)
    worker = asyncio.create_task(manager.worker())
    task = asyncio.current_task()
    assert task is not None
    worker.add_done_callback(lambda completed: task.cancel() if not completed.cancelled() else None)
    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, task.cancel)
    try:
        async with server:
            await server.serve_forever()
    except asyncio.CancelledError:
        pass
    finally:
        failure = worker.exception() if worker.done() and not worker.cancelled() else None
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
        await manager.close()
        if failure is not None:
            raise RuntimeError("sandbox_worker_failed") from failure


if __name__ == "__main__":
    asyncio.run(main())
