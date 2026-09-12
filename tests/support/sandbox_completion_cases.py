"""Durable completion delivery without running Docker or sending QQ messages."""

import json
import time
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from qq_ai_bot.sandbox.completions import MAX_UNACKNOWLEDGED, PAGE_BYTES
from qq_ai_bot.sandbox.manager import Manager
from qq_ai_bot.workspace.store import WorkspaceStore


def pending_job(manager, request_id=None):
    identity = str(uuid4())
    with manager.db:
        manager.db.execute(
            "INSERT INTO jobs VALUES (?,?,?,?,?,?,?)",
            (identity, request_id or identity, "hash", "{}", "queued", "{}", time.time()),
        )
    return identity


async def completion_delivery_cases(root):
    store = WorkspaceStore(root / "workspace", capacity=8)

    def open_manager():
        return Manager(root / "manager", store, "test", "internal", "proxy")

    manager = open_manager()
    run_id = pending_job(manager, "original-turn:original-call")
    # A failed outbox insert must roll back the terminal state too.
    with patch.object(manager.completions, "record", side_effect=RuntimeError("disk failure")):
        with pytest.raises(RuntimeError, match="disk failure"):
            manager.finish(run_id, "succeeded", {"output": "done"})
    assert manager.get(run_id)["status"] == "queued"
    assert manager.completions.pending()["events"] == []
    assert manager.finish(run_id, "succeeded", {"output": "done"})
    assert not manager.finish(run_id, "failed", {"error": "late failure"})
    assert not manager.finish(run_id, "running", {})
    request = {"method": "list_code_completions", "args": {}}
    events = await manager.handle(request)
    assert events == await manager.handle(request)  # reading never consumes
    assert events["events"][0]["request_id"] == "original-turn:original-call"
    assert events["events"][0]["result"]["status"] == "succeeded"
    manager.db.close()
    manager = open_manager()
    assert await manager.handle(request) == events
    # Existing 24h job cleanup must not discard an unacknowledged completion.
    with manager.db:
        manager.db.execute("DELETE FROM jobs WHERE id=?", (run_id,))
    assert await manager.handle(request) == events
    ack = {"method": "ack_code_completion", "args": {"run_id": run_id}}
    assert (await manager.handle(ack))["acknowledged"]
    assert (await manager.handle(ack))["acknowledged"]
    assert (await manager.handle(request))["events"] == []
    recovering = pending_job(manager)
    manager.finish(recovering, "running", {})
    with (
        patch.object(manager, "command", AsyncMock(side_effect=[(0, b""), (1, b"")])),
        patch.object(manager, "cleanup", AsyncMock()),
    ):
        await manager.recover()
    event = (await manager.handle(request))["events"][0]
    assert event["run_id"] == recovering
    assert event["result"]["error"] == "manager_restarted"
    manager.completions.acknowledge(recovering)
    # Pages are bounded by bytes as well as count, including JSON escaping.
    for _ in range(3):
        identity = pending_job(manager)
        manager.finish(identity, "succeeded", {"output": "鲸" * 32768})
    page = await manager.handle(request)
    assert len(page["events"]) == 1 and page["has_more"]
    assert len(json.dumps(page).encode()) <= PAGE_BYTES
    next_page = manager.completions.pending(after=page["next_cursor"])
    assert next_page["events"][0]["run_id"] != page["events"][0]["run_id"]
    assert next_page["next_cursor"] > page["next_cursor"]
    # Merely scanning past an unacknowledged event does not remove it.
    assert manager.completions.pending()["events"] == page["events"]
    for invalid in (-1, True, "1"):
        assert manager.completions.pending(after=invalid)["error"] == "invalid_arguments"
    for invalid in (0, 21, True, "1"):
        assert manager.completions.pending(invalid)["error"] == "invalid_arguments"
    with manager.db:
        manager.db.execute("DELETE FROM completion_outbox")
        for _ in range(MAX_UNACKNOWLEDGED - 1):
            manager.completions.record(str(uuid4()), "request", {})
    reserved = pending_job(manager)
    assert not manager.completions.reserve_available()
    manager.ready = True
    submission = {"method": "run_python", "request_id": "blocked", "args": {"code": "pass"}}
    assert (await manager.handle(submission))["error"] == "completion_backlog_full"
    assert manager.finish(reserved, "cancelled", {})  # reserved capacity remains usable
    assert not manager.completions.reserve_available()
    manager.completions.acknowledge(reserved)
    assert manager.completions.reserve_available()
    manager.db.close()
