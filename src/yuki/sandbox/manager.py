"""Trusted Linux host daemon. Not mounted into, or executed by, the Bot container.

Only this process controls Docker. All paths, images and resource options are
operator configuration; untrusted requests contain code and artifact IDs only.
"""

from __future__ import annotations

import argparse
import asyncio
import json
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


def desktop_session(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"true", "1", "yes", "on"}:
        return True
    if normalized in {"false", "0", "no", "off"}:
        return False
    raise argparse.ArgumentTypeError("desktop-session must be true or false")


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--socket", type=Path, required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--name", default="yuki-environment")
    parser.add_argument("--persistent-home", type=Path, required=True)
    parser.add_argument("--migrate-only", action="store_true")
    parser.add_argument("--network", default="yuki-sandbox-egress")
    parser.add_argument("--proxy", default="")
    parser.add_argument("--resolv-conf", type=Path)
    parser.add_argument(
        "--media-cache", type=Path, default=os.environ.get("YUKI_MANAGER_MEDIA_CACHE") or None
    )
    parser.add_argument(
        "--acp-config", type=Path, default=os.environ.get("YUKI_MANAGER_ACP_CONFIG")
    )
    parser.add_argument(
        "--desktop-session",
        type=desktop_session,
        default=os.environ.get("YUKI_MANAGER_DESKTOP_SESSION", "true"),
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
        acp_config=args.acp_config,
        media_cache=args.media_cache,
        desktop_session=args.desktop_session,
        name=args.name,
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
    server = await serve_unix(manager.serve, str(args.socket), limit=262144)
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
