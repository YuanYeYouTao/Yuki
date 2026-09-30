"""Physical SQLite fences and preparation cancellation, without Docker."""

import asyncio
import hashlib
import json
import sqlite3
import threading
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from tests.support.sandbox_completion_cases import pending_job

from qq_ai_bot.sandbox.manager import Manager
from qq_ai_bot.web.bridge_state import BridgeState
from qq_ai_bot.web.models import WebSearchResponse
from qq_ai_bot.workspace.store import WorkspaceError, WorkspaceStore


@pytest.mark.parametrize("operation", ["write", "snapshot"])
def test_committed_blob_survives_lost_commit_ack(tmp_path, monkeypatch, operation):
    store = WorkspaceStore(tmp_path / "artifacts")
    original = store.write("initial", b"old")
    identity = original["artifact_id"] if operation == "write" else str(uuid4())
    connect = sqlite3.connect

    class AckLostConnection(sqlite3.Connection):
        def commit(self):
            changed = self.total_changes > 0
            super().commit()
            if changed:
                raise OSError("commit acknowledgement lost")

    def fault_connection(*args, **kwargs):
        return connect(*args, **kwargs, factory=AckLostConnection)

    monkeypatch.setattr("qq_ai_bot.workspace.store.sqlite3.connect", fault_connection)
    with pytest.raises(OSError, match="commit acknowledgement lost"):
        if operation == "write":
            store.write("new", b"committed", artifact_id=identity, expected_revision=1)
        else:
            source = tmp_path / "source"
            source.write_bytes(b"committed")
            with source.open("rb") as stream:
                store.snapshot(stream.fileno(), "new", artifact_id=identity)
    monkeypatch.setattr("qq_ai_bot.workspace.store.sqlite3.connect", connect)
    metadata, data = store.read_bytes(identity)
    assert data == b"committed"
    assert metadata["revision"] == (2 if operation == "write" else 1)
    assert metadata["immutable"] == (operation == "snapshot")
    store.cleanup()
    assert store.read_bytes(identity)[1] == b"committed"
    assert not list(store.root.glob("*.pending"))


def test_file_hash_and_fsync_release_physical_writer(tmp_path, monkeypatch):
    store = WorkspaceStore(tmp_path / "artifacts")
    original = store.write("first", b"original")
    sha256, fsync = hashlib.sha256, __import__("os").fsync
    checks = []

    def writer_available():
        with sqlite3.connect(store.root / "manifest.sqlite3", timeout=0) as db:
            db.execute("BEGIN IMMEDIATE")
            db.rollback()
        checks.append(True)

    def checked_hash(data=b""):
        writer_available()
        return sha256(data)

    def checked_fsync(descriptor):
        writer_available()
        return fsync(descriptor)

    monkeypatch.setattr("qq_ai_bot.workspace.store.hashlib.sha256", checked_hash)
    monkeypatch.setattr("qq_ai_bot.workspace.store.os.fsync", checked_fsync)
    assert store.read_bytes(original["artifact_id"])[1] == b"original"
    batch = store.publish_batch([("one", b"1"), ("two", b"2")])
    assert len(batch) == 2
    source = tmp_path / "source"
    source.write_bytes(b"snapshot")
    with source.open("rb") as stream:
        assert store.snapshot(stream.fileno(), "snapshot")["immutable"]
    assert len(checks) >= 6
    checks.clear()
    store.write("renamed", b"original", artifact_id=original["artifact_id"], expected_revision=1)
    assert len(checks) == 1  # Identical contents never prepare/fsync a replacement.


def test_artifact_reads_and_cache_hits_do_not_take_writer(tmp_path, monkeypatch):
    store = WorkspaceStore(tmp_path / "artifacts")
    artifact = store.write("name", b"hello")
    with sqlite3.connect(store.root / "manifest.sqlite3") as writer:
        writer.execute("BEGIN IMMEDIATE")
        assert store.read_bytes(artifact["artifact_id"])[1] == b"hello"
        assert store.list()["items"][0]["artifact_id"] == artifact["artifact_id"]
        writer.rollback()
    cache = BridgeState(tmp_path / "cache.sqlite3")
    response = WebSearchResponse("query", (), None, 0)
    cache.access("key", response)
    with sqlite3.connect(cache.path) as writer:
        writer.execute("BEGIN IMMEDIATE")
        assert cache.access("key") == response
        monkeypatch.setattr("qq_ai_bot.web.bridge_state.time.time", lambda: 253402300799)
        assert cache.access("key") is None
        assert writer.execute("SELECT count(*) FROM cache").fetchone()[0] == 1
        writer.rollback()


def test_prepared_batch_rechecks_quota_and_rolls_back_whole_batch(tmp_path, monkeypatch):
    store = WorkspaceStore(tmp_path / "artifacts", capacity=6)
    prepared = store.prepare_batch([("one", b"123"), ("two", b"456")])
    store.write("competitor", b"x")
    with pytest.raises(WorkspaceError, match="workspace_full"):
        store.publish_prepared_batch(prepared)
    store.discard_prepared(prepared)
    assert len(store.list()["items"]) == 1
    store.delete(store.list()["items"][0]["artifact_id"], 1)
    prepared = store.prepare_batch([("one", b"123"), ("two", b"456")])
    publish = store._publish_file
    count = 0

    def fail_second(item):
        nonlocal count
        count += 1
        if count == 2:
            raise OSError("rename failed")
        publish(item)

    monkeypatch.setattr(store, "_publish_file", fail_second)
    with pytest.raises(OSError, match="rename failed"):
        store.publish_prepared_batch(prepared)
    store.discard_prepared(prepared)
    store.cleanup()
    assert store.list()["items"] == []
    assert list(store.root.glob("*.blob")) == []
    assert list(store.root.glob("*.pending")) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_task", [False, True])
async def test_manager_preparation_allows_cancellation_without_publication(
    tmp_path: Path, monkeypatch, cancel_task
):
    store = WorkspaceStore(tmp_path / "artifacts")
    manager = Manager(tmp_path / "jobs", store, "test", "internal", "proxy")
    identity = pending_job(manager)
    with manager.db:
        manager.db.execute(
            "UPDATE jobs SET payload=? WHERE id=?",
            (
                json.dumps({"code": "pass", "input_artifact_ids": [], "timeout_seconds": 1}),
                identity,
            ),
        )
    monkeypatch.setattr(manager, "stage_workspace", lambda _path: None)

    async def command(*args, **kwargs):
        if args[0] == "docker" and args[1] != "rm":
            outputs = manager.root / identity / "work" / "outputs"
            (outputs / "one").write_bytes(b"result")
        return 0, b"done"

    monkeypatch.setattr(manager, "command", command)
    prepare = store.prepare_batch
    started, release = threading.Event(), threading.Event()
    original_thread = threading.get_ident()

    def blocked_prepare(files):
        assert threading.get_ident() != original_thread
        prepared = prepare(files)
        started.set()
        assert release.wait(3)
        return prepared

    monkeypatch.setattr(store, "prepare_batch", blocked_prepare)
    publish = AsyncMock()  # Any invocation indicates a cancelled batch was exposed.
    monkeypatch.setattr(store, "publish_prepared_batch", publish)
    task = asyncio.create_task(manager.execute(identity))
    try:
        assert await asyncio.to_thread(started.wait, 3)
        if cancel_task:
            task.cancel()
        else:
            manager.cancelled.add(identity)
            manager.finish(identity, "cancelled", {})
        release.set()
        if cancel_task:
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            await task
            assert manager.get(identity)["status"] == "cancelled"
        publish.assert_not_called()
        assert store.list()["items"] == []
        assert list(store.root.glob("*.pending")) == []
    finally:
        release.set()
        manager.db.close()
