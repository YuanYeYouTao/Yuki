"""Explicit offline cleanup preserves real references and ordinary persistent originals."""

import hashlib
import json
import os
import sqlite3
from tempfile import TemporaryFile
from uuid import NAMESPACE_URL, uuid5

import pytest
from scripts.retire_media_snapshots import audit

from yuki.workspace.short_state import ShortState
from yuki.workspace.store import WorkspaceError, WorkspaceStore

pytestmark = pytest.mark.skipif(
    os.name != "posix", reason="Offline source verification uses safe FDs"
)


def snapshot_bytes(store, name, content, *, source_key, artifact_id=None):
    with TemporaryFile() as stream:
        stream.write(content)
        stream.flush()
        return store.snapshot(stream.fileno(), name, source_key=source_key, artifact_id=artifact_id)


def offline_copy(tmp_path):
    database = tmp_path / "bot.sqlite3"
    manager = tmp_path / "manager.sqlite3"
    home = tmp_path / "persistent-workspace"
    home.mkdir()
    with sqlite3.connect(database) as db:
        db.executescript(
            "CREATE TABLE social_operations(id TEXT, payload_json TEXT, result_json TEXT);"
            "CREATE TABLE runtime_protocol_objects(sha256 TEXT,byte_size INTEGER);"
            "CREATE TABLE runtime_protocol_refs(work_id TEXT,sha256 TEXT);"
            "CREATE TABLE tool_artifacts(relative_path TEXT,sha256 TEXT,"
            "byte_size INTEGER,deleting INT);"
        )
    with sqlite3.connect(manager) as db:
        db.executescript(
            "CREATE TABLE workspace_migrations(artifact_id TEXT,path TEXT,sha256 TEXT);"
            "CREATE TABLE environment_mutations(request_id TEXT,digest TEXT,result TEXT);"
        )
    store = WorkspaceStore(tmp_path / "artifacts")
    return store, {
        "workspace": store.root,
        "home_workspace": home,
        "database": database,
        "manager_database": manager,
        "protocol_root": tmp_path / "work-protocol",
        "tool_artifacts": tmp_path / "tool_artifacts",
    }


def legacy_sticker(store, args, content, *, name):
    digest = hashlib.sha256(content).hexdigest()
    identity = str(uuid5(NAMESPACE_URL, f"yuki:sticker:test-platform:{digest}"))
    artifact = snapshot_bytes(
        store,
        name,
        content,
        artifact_id=identity,
        source_key=f"sticker:test-platform:{digest}",
    )
    relative = f"stickers/test-platform/files/{name}"
    path = args["home_workspace"] / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    index = args["home_workspace"] / f"stickers/test-platform/index/{identity}.json"
    index.parent.mkdir(parents=True, exist_ok=True)
    index.write_text(
        json.dumps(
            {
                "id": identity,
                "sha256": digest,
                "relative_path": relative,
                "tags": ["user-maintained tag"],
            }
        ),
        encoding="utf-8",
    )
    receipt = {
        "artifact_id": identity,
        "path": "/workspace/" + relative,
        "version": digest,
        "size": len(content),
    }
    with sqlite3.connect(args["manager_database"]) as db:
        db.execute("INSERT INTO workspace_migrations VALUES(?,?,?)", (identity, relative, digest))
        db.execute(
            "INSERT INTO environment_mutations VALUES(?,?,?)",
            (f"old-copy:{identity}", "original-digest", json.dumps(receipt)),
        )
    return artifact, path, index


def test_read_only_then_retire_only_unreferenced_duplicate_and_keep_original_receipts(tmp_path):
    store, args = offline_copy(tmp_path)
    unused, original, index = legacy_sticker(store, args, b"unused sticker", name="renamed.png")
    referenced, delivered, delivered_index = legacy_sticker(
        store, args, b"delivered sticker", name="delivered.png"
    )
    ordinary = snapshot_bytes(
        store, "published.txt", b"separately published", source_key="send:123"
    )
    with sqlite3.connect(args["database"]) as db:
        db.execute(
            "INSERT INTO social_operations VALUES(?,?,?)",
            (
                "original-unknown-send",
                json.dumps({"artifact_id": referenced["artifact_id"]}),
                '{"status":"unknown"}',
            ),
        )
    with sqlite3.connect(args["manager_database"]) as db:
        original_receipts = db.execute("SELECT * FROM environment_mutations").fetchall()
    index_content = index.read_bytes()
    report = audit(**args)
    assert report["candidate_count"] == 2 and report["eligible_count"] == 1
    assert report["removed_count"] == 0 and store.read_bytes(unused["artifact_id"])[1]
    results = {item["id"]: item for item in report["candidates"]}
    assert results[unused["artifact_id"]]["keep_reasons"] == []
    assert results[referenced["artifact_id"]]["keep_reasons"] == ["referenced"]

    applied = audit(**args, apply=True)
    assert applied["removed_count"] == 1 and applied["removed_bytes"] == len(b"unused sticker")
    with pytest.raises(WorkspaceError, match="artifact_not_found"):
        store.read_bytes(unused["artifact_id"])
    assert store.read_bytes(referenced["artifact_id"])[1] == delivered.read_bytes()
    assert store.read_bytes(ordinary["artifact_id"])[1] == b"separately published"
    assert original.read_bytes() == b"unused sticker" and index.read_bytes() == index_content
    assert delivered_index.exists()
    with sqlite3.connect(args["manager_database"]) as db:
        assert db.execute("SELECT * FROM environment_mutations").fetchall() == original_receipts
        assert [row[0] for row in db.execute("SELECT artifact_id FROM workspace_migrations")] == [
            referenced["artifact_id"]
        ]


@pytest.mark.parametrize("private_kind", ["protocol", "tool"])
def test_private_original_tool_result_or_protocol_reference_keeps_snapshot(tmp_path, private_kind):
    store, args = offline_copy(tmp_path)
    artifact, _original, _index = legacy_sticker(
        store, args, b"selected sticker", name="selected.png"
    )
    content = json.dumps({"original_result": {"artifact_id": artifact["artifact_id"]}}).encode()
    digest = hashlib.sha256(content).hexdigest()
    with sqlite3.connect(args["database"]) as db:
        if private_kind == "protocol":
            path = args["protocol_root"] / digest[:2] / digest[2:]
            db.execute("INSERT INTO runtime_protocol_objects VALUES(?,?)", (digest, len(content)))
            db.execute("INSERT INTO runtime_protocol_refs VALUES(?,?)", ("original-work", digest))
        else:
            path = args["tool_artifacts"] / "original-result.json"
            db.execute(
                "INSERT INTO tool_artifacts VALUES(?,?,?,0)", (path.name, digest, len(content))
            )
    path.parent.mkdir(parents=True)
    path.write_bytes(content)
    report = audit(**args, apply=True)
    assert report["eligible_count"] == report["removed_count"] == 0
    assert report["candidates"][0]["keep_reasons"] == ["referenced"]
    assert path.read_bytes() == content and store.read_bytes(artifact["artifact_id"])[1]


def test_missing_owned_evidence_or_changed_persistent_file_never_claims_duplicate_is_disposable(
    tmp_path,
):
    store, args = offline_copy(tmp_path)
    artifact, original, _index = legacy_sticker(store, args, b"selected", name="selected.png")
    original.write_bytes(b"user changed the ordinary original")
    with sqlite3.connect(args["database"]) as db:
        digest = hashlib.sha256(b"missing private original").hexdigest()
        db.execute("INSERT INTO runtime_protocol_objects VALUES(?,?)", (digest, 24))
        db.execute("INSERT INTO runtime_protocol_refs VALUES(?,?)", ("original-work", digest))
    report = audit(**args, apply=True)
    assert report["removed_count"] == 0 and report["reference_evidence_incomplete"]
    assert set(report["candidates"][0]["keep_reasons"]) == {
        "reference_evidence_incomplete",
        "no_matching_persistent_file",
    }
    assert store.read_bytes(artifact["artifact_id"])[1] == b"selected"
    assert original.read_bytes() == b"user changed the ordinary original"


def test_explicit_publication_receipt_is_not_mistaken_for_a_retired_checkout(tmp_path):
    store, args = offline_copy(tmp_path)
    artifact, _original, _index = legacy_sticker(store, args, b"published", name="published.png")
    published = artifact | {"path": "/workspace/published.png", "version": artifact["sha256"]}
    with sqlite3.connect(args["manager_database"]) as db:
        db.execute(
            "INSERT INTO environment_mutations VALUES(?,?,?)",
            ("actual-publication", "original-publish-digest", json.dumps(published)),
        )
    report = audit(**args, apply=True)
    assert report["removed_count"] == 0
    assert report["candidates"][0]["keep_reasons"] == ["referenced"]
    assert store.read_bytes(artifact["artifact_id"])[1] == b"published"


def test_plain_attachment_intermediate_can_retire_without_deleting_saved_file(tmp_path):
    store, args = offline_copy(tmp_path)
    content = b"saved file bytes"
    original = args["home_workspace"] / "saved.txt"
    original.write_bytes(content)
    artifact = snapshot_bytes(
        store, "saved.txt", content, source_key="attachment:conversation:12:0"
    )
    receipt = {
        "artifact_id": artifact["artifact_id"],
        "path": "/workspace/saved.txt",
        "version": artifact["sha256"],
        "size": artifact["size"],
    }
    with sqlite3.connect(args["manager_database"]) as db:
        db.execute(
            "INSERT INTO environment_mutations VALUES(?,?,?)",
            ("original-save", "original-save-digest", json.dumps(receipt)),
        )
    report = audit(**args, apply=True)
    assert report["removed_count"] == 1
    assert original.read_bytes() == content
    with sqlite3.connect(args["manager_database"]) as db:
        assert db.execute("SELECT result FROM environment_mutations").fetchone()[0] == json.dumps(
            receipt
        )


def test_manifest_short_state_reference_keeps_snapshot_and_original_text(tmp_path):
    store, args = offline_copy(tmp_path)
    artifact, _original, _index = legacy_sticker(store, args, b"remembered", name="remembered.png")
    state = ShortState(store)
    state.update({"slot": 1, "text": f"原文件快照 {artifact['artifact_id']}"})
    original_state = state.snapshot()
    report = audit(**args, apply=True)
    assert report["removed_count"] == 0
    candidate = report["candidates"][0]
    assert candidate["keep_reasons"] == ["referenced"]
    assert any("manifest.sqlite3:short_state.text" in value for value in candidate["references"])
    assert state.snapshot() == original_state
    assert store.read_bytes(artifact["artifact_id"])[1] == b"remembered"
