"""Upgrade reads and retires original server ACP jobs without admitting new ones."""

import asyncio
import json
import time
from uuid import uuid4

import pytest

from yuki.sandbox.legacy_acp_protocol import atomic_json, operation_key, read_json
from yuki.sandbox.persistent import PersistentManager
from yuki.sandbox.protocol import read_protocol_line
from yuki.workspace.store import WorkspaceStore


def original_manager(tmp_path):
    manager = PersistentManager(
        tmp_path / "manager",
        WorkspaceStore(tmp_path / "artifacts"),
        "image",
        "network",
        "",
        tmp_path / "home",
        testing=True,
    )
    manager.ready = True
    peer, run = str(uuid4()), str(uuid4())
    with manager.db:
        for identity, request, kind, payload in (
            (peer, "original:peer", "acp_peer", {}),
            (run, "original", "acp_prompt", {"peer_id": peer, "prompt": "original input"}),
        ):
            manager.db.execute(
                "INSERT INTO jobs VALUES (?,?,?,?,?,?,?)",
                (identity, request, "digest", json.dumps(payload), "running", "{}", time.time()),
            )
            manager.db.execute(
                "INSERT INTO environment_jobs(id,kind,dispatched) VALUES (?,?,1)",
                (identity, kind),
            )
    root = manager.legacy_acp.root(peer)
    atomic_json(root / "peer_metadata.json", {"session_id": "native-original"})
    return manager, peer, run


async def test_old_completion_preserves_original_run_and_outbox(tmp_path):
    manager, peer, run = original_manager(tmp_path)
    try:
        root = manager.legacy_acp.root(peer)
        atomic_json(
            root / "rpc/results" / f"{operation_key('original')}.json",
            {"status": "succeeded", "output": "original result"},
        )
        result = await manager.handle({"method": "acp_prompt_query", "args": {"run_id": run}})
        assert result["run_id"] == run and result["status"] == "succeeded"
        assert result["peer_id"] == peer and result["kind"] == "acp_prompt"
        assert result["output"] == "original result" and result["pending"] is False
        assert manager.completions.pending()["events"][0]["run_id"] == run
        assert manager.db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 2
    finally:
        await manager.close()


async def test_legacy_cancel_targets_original_prompt_not_peer_or_other_run(tmp_path):
    manager, peer, run = original_manager(tmp_path)
    try:
        result = await manager.handle(
            {"method": "acp_prompt_cancel", "args": {"run_id": run}, "request_id": "cancel"}
        )
        assert result["run_id"] == run and result["cancel_requested"] is True
        original = read_json(
            manager.legacy_acp.root(peer) / "rpc/inbox" / f"{operation_key('cancel')}.json"
        )
        assert original["method"] == "cancel" and original["run_id"] == run
        assert manager.legacy_acp.view(peer)["status"] == "running"
        again = await manager.handle(
            {"method": "acp_prompt_cancel", "args": {"run_id": run}, "request_id": "cancel"}
        )
        assert again["operation_id"] == result["operation_id"]
        assert manager.db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 3
    finally:
        await manager.close()


@pytest.mark.parametrize("method", ["acp_agents", "acp_peer_open", "acp_prompt_submit"])
async def test_retired_server_acp_cannot_admit_another_job(tmp_path, method):
    manager, _, _ = original_manager(tmp_path)
    try:
        assert await manager.handle({"method": method, "args": {}, "request_id": "new"}) == {
            "error": "unknown_method",
            "retryable": False,
        }
        assert manager.db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 2
    finally:
        await manager.close()


async def test_manager_frame_uses_complete_line_across_buffer_limits():
    reader = asyncio.StreamReader(limit=16)
    original = json.dumps({"output": "内容" * 1000}).encode() + b"\n"
    reader.feed_data(original)
    assert await read_protocol_line(reader) == original
