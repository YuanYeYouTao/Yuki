"""Read-only Manager verification shared by Bot setup and the minimal host venv."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
from typing import Any


def _artifact_store_writable(path: Path) -> bool:
    return path.is_dir() and os.access(
        path, os.W_OK | os.X_OK, effective_ids=os.access in os.supports_effective_ids
    )


def _artifact_lock_writable(path: Path) -> bool:
    try:
        descriptor = os.open(path / ".artifact.lock", os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        return False
    os.close(descriptor)
    return True


async def check_environment(
    socket: Path, artifact_store: Path, *, expected_uid: int = 10001
) -> dict[str, Any]:
    """Read status without importing Bot dependencies or starting any execution."""
    geteuid = getattr(os, "geteuid", None)
    if geteuid is None:
        return {"ok": False, "error": "linux_host_required"}
    if geteuid() != expected_uid:
        return {"ok": False, "error": "bot_uid_required", "expected_uid": expected_uid}
    if not await asyncio.to_thread(_artifact_store_writable, artifact_store):
        return {"ok": False, "error": "artifact_store_not_writable"}
    if not await asyncio.to_thread(_artifact_lock_writable, artifact_store):
        return {"ok": False, "error": "artifact_lock_not_writable"}
    try:
        async with asyncio.timeout(7):
            reader, writer = await asyncio.open_unix_connection(str(socket), limit=262144)
            try:
                writer.write(
                    b'{"method":"environment_status","args":{},'
                    b'"request_id":"deployment-environment-status"}\n'
                )
                await writer.drain()
                status = json.loads(await reader.readline())
            finally:
                writer.close()
                await writer.wait_closed()
    except (AttributeError, OSError, ValueError, TimeoutError, NotImplementedError):
        return {"ok": False, "error": "manager_connection_failed"}
    if not isinstance(status, dict) or status.get("error"):
        return {"ok": False, "error": "manager_connection_failed"}
    if status.get("ready") is not True or status.get("workspace") != "/workspace":
        return {"ok": False, "error": "persistent_environment_not_ready"}
    return {"ok": True, "uid": expected_uid, "manager": "connected", "workspace": "/workspace"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket", type=Path, required=True)
    parser.add_argument("--artifact-store", type=Path, required=True)
    args = parser.parse_args()
    result = asyncio.run(check_environment(args.socket, args.artifact_store))
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
