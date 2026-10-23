"""Free terminal files retain only media/publication/import bridges."""

import hashlib
import sqlite3
from pathlib import Path
from tempfile import TemporaryFile
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from tests.support.service_dependencies import workspace_service
from tests.support.workspace_snapshots import snapshot_bytes

from yuki.deployment_setup.environment_check import check_environment
from yuki.workspace.short_state import ShortState
from yuki.workspace.store import WorkspaceError, WorkspaceStore


@pytest.mark.parametrize(
    "name",
    [
        "workspace_list",
        "workspace_read",
        "workspace_write",
        "workspace_search",
        "workspace_patch",
        "workspace_mkdir",
        "workspace_move",
        "workspace_delete",
    ],
)
async def test_retired_file_operations_do_not_dispatch_to_manager(tmp_path, name):
    service = workspace_service(WorkspaceStore(tmp_path / "artifacts"))
    service.sandbox = SimpleNamespace(execute=AsyncMock())
    with pytest.raises(WorkspaceError, match="unknown_tool"):
        await service.execute(name, {"path": "work.txt", "artifact_id": str(uuid4())})
    service.sandbox.execute.assert_not_awaited()


def test_large_files_publish_and_read_above_former_file_and_total_quotas(tmp_path):
    store = WorkspaceStore(tmp_path / "artifacts")
    # Real file bytes, metadata, hashes and SQLite publication cross both old limits.
    size = 201 * 1024 * 1024
    with TemporaryFile() as source:
        source.truncate(size)
        snapshots = [store.snapshot(source.fileno(), f"result-{index}.bin") for index in range(3)]
    assert sum(item["size"] for item in snapshots) > 512 * 1024 * 1024
    original = snapshots[0]
    metadata, data = store.read_bytes(original["artifact_id"])
    assert len(data) == size
    assert metadata == original
    assert hashlib.sha256(data).hexdigest() == original["sha256"]
    with pytest.raises(WorkspaceError, match="artifact_too_large"):
        store.read_bytes(original["artifact_id"], max_bytes=20 * 1024 * 1024)


def test_more_than_one_thousand_snapshots_keep_reference_gc_and_short_state(tmp_path):
    store = WorkspaceStore(tmp_path / "artifacts")
    snapshots = [snapshot_bytes(store, f"result-{index}.txt", b"") for index in range(1001)]
    orphan = store.root / f"{uuid4()}.blob"
    orphan.write_bytes(b"unreferenced")
    state = ShortState(store)
    state.update({"slot": 1, "text": "terminal progress"})
    store.cleanup()
    assert not orphan.exists()
    assert len(list(store.root.glob("*.blob"))) == 1001
    assert state.snapshot()[0]["text"] == "terminal progress"
    metadata, data = store.read_bytes(snapshots[-1]["artifact_id"])
    assert metadata == snapshots[-1] and data == b""


def test_cleanup_keeps_original_referenced_blob_and_removes_missing_metadata(tmp_path):
    store = WorkspaceStore(tmp_path / "artifacts")
    original = snapshot_bytes(store, "kept.txt", b"original receipt bytes")
    missing = snapshot_bytes(store, "removed.txt", b"gone")
    with sqlite3.connect(store.root / "manifest.sqlite3") as db:
        blob = db.execute(
            "SELECT blob FROM artifacts WHERE id=?", (missing["artifact_id"],)
        ).fetchone()[0]
    (store.root / blob).unlink()
    store.cleanup()
    assert store.read_bytes(original["artifact_id"])[1] == b"original receipt bytes"
    with pytest.raises(WorkspaceError, match="artifact_not_found"):
        store.read_bytes(missing["artifact_id"])


async def test_environment_check_uses_existing_status_without_file_rpc(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "yuki.deployment_setup.environment_check.os.geteuid", lambda: 10001, raising=False
    )
    execute = AsyncMock(return_value={"ready": True, "workspace": "/workspace"})
    monkeypatch.setattr(
        "yuki.deployment_setup.environment_check.SandboxClient",
        lambda _socket: SimpleNamespace(execute=execute),
    )
    assert (await check_environment(Path("unused.sock"), tmp_path))["ok"]
    assert execute.await_count == 1
    assert execute.await_args.args[0] == "environment_status"
