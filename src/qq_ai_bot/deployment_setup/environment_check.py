"""Read-only verification of the existing Manager from the Bot's real identity."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

from qq_ai_bot.sandbox.client import SandboxClient


def _artifact_store_writable(path: Path) -> bool:
    return path.is_dir() and os.access(
        path, os.W_OK | os.X_OK, effective_ids=os.access in os.supports_effective_ids
    )


async def check_environment(
    socket: Path, artifact_store: Path, *, expected_uid: int = 10001
) -> dict[str, Any]:
    """Do not start a Manager, environment, model, job, or database connection."""
    geteuid = getattr(os, "geteuid", None)
    if geteuid is None:
        return {"ok": False, "error": "linux_host_required"}
    if geteuid() != expected_uid:
        return {"ok": False, "error": "bot_uid_required", "expected_uid": expected_uid}
    if not await asyncio.to_thread(_artifact_store_writable, artifact_store):
        return {"ok": False, "error": "artifact_store_not_writable"}
    client = SandboxClient(socket)
    status = await client.execute(
        "environment_status", {}, request_id="deployment-environment-status"
    )
    if status.get("error"):
        return {"ok": False, "error": "manager_connection_failed"}
    if status.get("ready") is not True or status.get("workspace") != "/workspace":
        return {"ok": False, "error": "persistent_environment_not_ready"}
    files = await client.execute(
        "workspace_list",
        {"path": "/workspace", "limit": 1},
        request_id="deployment-workspace-status",
    )
    if files.get("error") or not isinstance(files.get("items"), list):
        return {"ok": False, "error": "workspace_interface_unavailable"}
    # Raw status contains task/service identifiers, filenames and resource data.
    # The installation check publishes only the verified capability facts.
    return {"ok": True, "uid": expected_uid, "manager": "connected", "workspace": "/workspace"}
