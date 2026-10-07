"""Physical SQLite fences and preparation cancellation, without Docker."""

import hashlib
import sqlite3
from uuid import uuid4

import pytest
from tests.support.workspace_snapshots import snapshot_bytes

from qq_ai_bot.web.bridge_state import BridgeState
from qq_ai_bot.web.models import WebSearchResponse
from qq_ai_bot.workspace.store import WorkspaceStore


def test_committed_blob_survives_lost_commit_ack(tmp_path, monkeypatch):
    store = WorkspaceStore(tmp_path / "artifacts")
    snapshot_bytes(store, "initial", b"old")
    identity = str(uuid4())
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
        source = tmp_path / "source"
        source.write_bytes(b"committed")
        with source.open("rb") as stream:
            store.snapshot(stream.fileno(), "new", artifact_id=identity)
    monkeypatch.setattr("qq_ai_bot.workspace.store.sqlite3.connect", connect)
    metadata, data = store.read_bytes(identity)
    assert data == b"committed"
    assert metadata["revision"] == 1
    assert metadata["immutable"] is True
    store.cleanup()
    assert store.read_bytes(identity)[1] == b"committed"
    assert not list(store.root.glob("*.pending"))


def test_file_hash_and_fsync_release_physical_writer(tmp_path, monkeypatch):
    store = WorkspaceStore(tmp_path / "artifacts")
    original = snapshot_bytes(store, "first", b"original")
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
    batch = [snapshot_bytes(store, "one", b"1"), snapshot_bytes(store, "two", b"2")]
    assert len(batch) == 2
    source = tmp_path / "source"
    source.write_bytes(b"snapshot")
    with source.open("rb") as stream:
        assert store.snapshot(stream.fileno(), "snapshot")["immutable"]
    assert len(checks) >= 6


def test_artifact_reads_and_cache_hits_do_not_take_writer(tmp_path, monkeypatch):
    store = WorkspaceStore(tmp_path / "artifacts")
    artifact = snapshot_bytes(store, "name", b"hello")
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
