"""A new immutable publication should not revisit every permanent manifest row."""

import sqlite3
from contextlib import closing
from uuid import uuid4

from tests.support.workspace_snapshots import snapshot_bytes

from yuki.workspace.store import WorkspaceStore


def test_publication_work_does_not_scale_with_permanent_history(tmp_path, monkeypatch):
    store = WorkspaceStore(tmp_path / "artifacts")
    original = snapshot_bytes(store, "original.txt", b"original bytes")
    connect = sqlite3.connect
    costs = []
    previous = 1
    for size in (16, 640):
        # Reuse a valid immutable blob; only unrelated manifest cardinality changes.
        with closing(connect(store.root / "manifest.sqlite3")) as writer, writer:
            row = writer.execute(
                "SELECT * FROM artifacts WHERE id=?", (original["artifact_id"],)
            ).fetchone()
            for _ in range(size - previous):
                identity = str(uuid4())
                writer.execute(
                    "INSERT INTO artifacts VALUES (?,?,?,?,?,?,?,?,?)", (identity, *row[1:])
                )
                writer.execute(
                    "INSERT INTO artifact_snapshots VALUES (?,?)", (identity, "original-source")
                )
        steps = 0

        def tick():
            nonlocal steps
            steps += 1
            return 0

        def measured_connect(*args, **kwargs):
            db = connect(*args, **kwargs)
            db.set_progress_handler(tick, 1)
            return db

        with monkeypatch.context() as scoped:
            scoped.setattr("yuki.workspace.store.sqlite3.connect", measured_connect)
            published = snapshot_bytes(store, "new.txt", b"new bytes")
        costs.append(steps)
        assert store.read_bytes(published["artifact_id"])[1] == b"new bytes"
        previous = size
    assert store.read_bytes(original["artifact_id"])[1] == b"original bytes"
    # Relative complexity tolerance across SQLite versions, never an elapsed-time gate.
    assert costs[1] <= costs[0] * 4, costs
