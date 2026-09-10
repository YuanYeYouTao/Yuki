"""Shared scratch data expires without destroying valid or newer revisions."""

import os
from pathlib import Path

import pytest

from qq_ai_bot.workspace.store import WorkspaceError, WorkspaceStore


def test_workspace_expiry_revision_quota_and_file_integrity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = [1000.0]
    monkeypatch.setattr("qq_ai_bot.workspace.store.time.time", lambda: clock[0])
    store = WorkspaceStore(tmp_path / "scratch", ttl=100, capacity=12, max_file=10, max_objects=2)
    first = store.write("note.txt", b"hello")
    identity = first["artifact_id"]
    clock[0] += 20
    assert store.read(identity)["text"] == "hello"
    same = store.write("renamed.txt", b"hello", artifact_id=identity, expected_revision=1)
    assert same["expires_at"] == first["expires_at"] and same["revision"] == 2
    with pytest.raises(WorkspaceError, match="version_conflict"):
        store.write("note.txt", b"new", artifact_id=identity, expected_revision=1)
    second = store.write("more.txt", b"1234567")
    with pytest.raises(WorkspaceError, match="workspace_full"):
        store.write("full.txt", b"x")
    assert len(store.list()["items"]) == 2
    restarted = WorkspaceStore(store.root, ttl=100, capacity=12, max_file=10, max_objects=2)
    assert restarted.read(identity)["expires_at"] == first["expires_at"]
    for name in ("../escape", "/etc/passwd", "a\\b", "C:secret"):
        with pytest.raises(WorkspaceError, match="invalid_artifact_name"):
            store.write(name, b"x")
    with pytest.raises(WorkspaceError, match="invalid_artifact_id"):
        store.read("../manifest.sqlite3")
    clock[0] = 1101
    with pytest.raises(WorkspaceError, match="artifact_expired"):
        store.read(identity)
    assert store.cleanup() == 1
    assert store.read(second["artifact_id"])["text"] == "1234567"
    store.delete(second["artifact_id"], 1)
    assert store.list()["items"] == []
    linked = store.write("link.txt", b"link")
    blob = next(store.root.glob("*.blob"))
    os.link(blob, tmp_path / "hardlink")
    with pytest.raises(WorkspaceError, match="unsafe_artifact"):
        store.read(linked["artifact_id"])
