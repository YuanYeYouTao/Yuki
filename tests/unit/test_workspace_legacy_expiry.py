"""Retiring an unused option preserves original snapshots and old store activation."""

import sqlite3

import pytest
from tests.support.workspace_snapshots import snapshot_bytes

from qq_ai_bot.workspace.store import WorkspaceStore


@pytest.mark.parametrize("legacy_expiry", [1, 4_000_000_000])
def test_activation_preserves_legacy_snapshot_and_removes_expiry(tmp_path, legacy_expiry):
    store = WorkspaceStore(tmp_path / "artifacts")
    original = snapshot_bytes(store, "original.txt", b"retained original bytes")
    with sqlite3.connect(store.root / "manifest.sqlite3") as writer:
        writer.execute(
            "UPDATE artifacts SET expires_at=? WHERE id=?",
            (legacy_expiry, original["artifact_id"]),
        )
    restarted = WorkspaceStore(store.root)
    restarted.cleanup()
    metadata, data = restarted.read_bytes(original["artifact_id"])
    assert data == b"retained original bytes"
    assert metadata == original
    assert metadata["expires_at"] is None
    assert metadata["immutable"]
