"""A real legacy manifest upgrades once without changing immutable originals."""

import hashlib
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from threading import Barrier
from uuid import uuid4

import pytest
from tests.support.workspace_snapshots import snapshot_bytes

from yuki.workspace.store import WorkspaceError, WorkspaceStore


def legacy_manifest(root, *, expiry=1, source_key=True):
    root.mkdir()
    identity, blob = str(uuid4()), f"{uuid4()}.blob"
    data = b"retained original bytes"
    (root / blob).write_bytes(data)
    row = (
        identity,
        "original.txt",
        blob,
        hashlib.sha256(data).hexdigest(),
        len(data),
        7,
        123.0,
        456.0,
    )
    with closing(sqlite3.connect(root / "manifest.sqlite3")) as writer, writer:
        writer.execute(
            "CREATE TABLE artifacts (id TEXT PRIMARY KEY,name TEXT NOT NULL,blob TEXT NOT NULL,"
            "sha256 TEXT NOT NULL,size INTEGER NOT NULL,revision INTEGER NOT NULL,"
            "created_at REAL NOT NULL,modified_at REAL NOT NULL,expires_at REAL NOT NULL)"
        )
        writer.execute("INSERT INTO artifacts VALUES (?,?,?,?,?,?,?,?,?)", (*row, expiry))
        writer.execute(
            "CREATE TABLE artifact_snapshots (id TEXT PRIMARY KEY"
            + (",source_key TEXT)" if source_key else ")")
        )
        writer.execute(
            "INSERT INTO artifact_snapshots VALUES (?,?)"
            if source_key
            else "INSERT INTO artifact_snapshots VALUES (?)",
            (identity, "original-source") if source_key else (identity,),
        )
    return identity, row, data


@pytest.mark.parametrize("legacy_expiry", [1, 4_000_000_000])
@pytest.mark.parametrize("source_key", [False, True])
def test_activation_preserves_legacy_snapshot_and_removes_expiry(
    tmp_path, legacy_expiry, source_key
):
    root = tmp_path / "artifacts"
    identity, row, data = legacy_manifest(root, expiry=legacy_expiry, source_key=source_key)
    store = WorkspaceStore(root)
    metadata, actual = store.read_bytes(identity)
    assert actual == data
    assert metadata == {
        "artifact_id": identity,
        "name": row[1],
        "sha256": row[3],
        "size": row[4],
        "revision": row[5],
        "created_at": row[6],
        "modified_at": row[7],
        "immutable": True,
    }
    assert store.snapshot_source(identity) == ("original-source" if source_key else None)
    with closing(sqlite3.connect(root / "manifest.sqlite3")) as reader:
        assert "expires_at" not in {
            item[1] for item in reader.execute("PRAGMA table_info(artifacts)")
        }
        assert tuple(reader.execute("SELECT * FROM artifacts").fetchone()) == row
    assert WorkspaceStore(root).read_bytes(identity) == (metadata, actual)


def test_concurrent_owners_upgrade_the_same_legacy_manifest(tmp_path):
    root = tmp_path / "artifacts"
    identity, row, data = legacy_manifest(root, source_key=False)
    barrier = Barrier(2)

    def opened():
        barrier.wait()
        store = WorkspaceStore(root)
        return store.read_bytes(identity), store.snapshot_source(identity)

    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(lambda _: opened(), range(2)))
    assert results[0] == results[1]
    assert results[0][0][1] == data and results[0][0][0]["revision"] == row[5]
    assert results[0][1] is None


def test_failed_upgrade_rolls_back_both_schema_changes_and_closes(tmp_path, monkeypatch):
    root = tmp_path / "artifacts"
    identity, row, data = legacy_manifest(root, source_key=False)
    connect = sqlite3.connect
    connections = []

    class FailingConnection(sqlite3.Connection):
        def execute(self, sql, *args):
            if sql == "ALTER TABLE artifacts DROP COLUMN expires_at":
                raise sqlite3.OperationalError("injected schema failure")
            return super().execute(sql, *args)

    def failing_connect(*args, **kwargs):
        connection = connect(*args, **kwargs, factory=FailingConnection)
        connections.append(connection)
        return connection

    with monkeypatch.context() as scoped:
        scoped.setattr("yuki.workspace.store.sqlite3.connect", failing_connect)
        with pytest.raises(sqlite3.OperationalError, match="injected schema failure"):
            WorkspaceStore(root)
    for connection in connections:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            connection.execute("SELECT 1")
    with closing(connect(root / "manifest.sqlite3")) as reader:
        assert "source_key" not in {
            item[1] for item in reader.execute("PRAGMA table_info(artifact_snapshots)")
        }
        assert reader.execute("SELECT * FROM artifacts").fetchone() == (*row, 1)
    assert WorkspaceStore(root).read_bytes(identity)[1] == data


def test_open_owner_business_operations_do_not_prepare_schema_again(tmp_path, monkeypatch):
    store = WorkspaceStore(tmp_path / "artifacts")
    original = snapshot_bytes(store, "original.txt", b"original bytes")
    connect, statements = sqlite3.connect, []

    def traced_connect(*args, **kwargs):
        connection = connect(*args, **kwargs)
        connection.set_trace_callback(statements.append)
        return connection

    with monkeypatch.context() as scoped:
        scoped.setattr("yuki.workspace.store.sqlite3.connect", traced_connect)
        assert store.read_bytes(original["artifact_id"])[1] == b"original bytes"
        assert store.snapshot_source(original["artifact_id"]) is None
        snapshot_bytes(store, "later.txt", b"later bytes")
        store.cleanup()
    assert not any(
        "SQLITE_MASTER" in sql.upper() or sql.upper().startswith(("PRAGMA", "CREATE", "ALTER"))
        for sql in statements
    ), statements


def test_open_owner_still_rejects_a_replaced_hardlinked_index(tmp_path):
    store = WorkspaceStore(tmp_path / "artifacts")
    original = snapshot_bytes(store, "original.txt", b"original bytes")
    index = store.root / "manifest.sqlite3"
    (store.root / "linked.sqlite3").hardlink_to(index)
    with pytest.raises(WorkspaceError, match="unsafe_workspace_index"):
        store.read_bytes(original["artifact_id"])
