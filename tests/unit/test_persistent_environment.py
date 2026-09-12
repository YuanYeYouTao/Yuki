"""Boundaries for persistent files, migration, and one declaration contract."""

import hashlib
import json
import os
import sqlite3
from pathlib import Path

import pytest

from qq_ai_bot.sandbox.client import sandbox_tools
from qq_ai_bot.sandbox.environment_tools import SANDBOX_TOOLS
from qq_ai_bot.workspace.files import FileWorkspace
from qq_ai_bot.workspace.store import WorkspaceError, WorkspaceStore
from qq_ai_bot.workspace.tools import WORKSPACE_TOOLS, workspace_tools


def test_snapshots_are_persistent_immutable_and_old_schema_compatible(tmp_path: Path, monkeypatch):
    store = WorkspaceStore(tmp_path / "artifacts")
    old = store.write("old.txt", b"old")
    source = tmp_path / "source.txt"
    source.write_bytes(b"snapshot")
    with source.open("rb") as stream:
        frozen = store.snapshot(stream.fileno(), "published.txt")
    source.write_bytes(b"changed")
    monkeypatch.setattr("qq_ai_bot.workspace.store.time.time", lambda: 250000000000)
    store.cleanup()
    assert store.read(old["artifact_id"])["text"] == "old"
    assert store.read(frozen["artifact_id"])["text"] == "snapshot"
    with pytest.raises(WorkspaceError, match="immutable"):
        store.write("published.txt", b"new", artifact_id=frozen["artifact_id"], expected_revision=1)
    with sqlite3.connect(store.root / "manifest.sqlite3") as db:
        assert len(db.execute("PRAGMA table_info(artifacts)").fetchall()) == 9


def test_shared_declaration_sets_and_mutable_reads():
    assert {tool.name for tool in sandbox_tools()} == SANDBOX_TOOLS
    assert {tool.name for tool in workspace_tools()} == WORKSPACE_TOOLS
    assert all(not tool.result_cacheable for tool in workspace_tools())
    assert all(not tool.result_cacheable for tool in sandbox_tools())


@pytest.mark.skipif(os.name != "posix", reason="Host file confinement uses POSIX directory FDs")
def test_shared_files_versions_unicode_moves_and_escape(tmp_path: Path):
    workspace = FileWorkspace(tmp_path)
    first = workspace.write("项目/笔记.txt", "文件工具".encode())
    assert (tmp_path / "项目/笔记.txt").read_text() == "文件工具"
    (tmp_path / "项目/笔记.txt").write_text("终端修改")
    second = workspace.read(first["path"])
    assert second["text"] == "终端修改"
    with pytest.raises(WorkspaceError, match="version_conflict"):
        workspace.write(first["path"], b"lost update", first["version"])
    updated = workspace.patch(first["path"], "终端", "保留", second["version"])
    workspace.move(first["path"], "新目录/笔记.txt", updated["version"])
    assert workspace.search("保留")["matches"][0]["path"] == "/workspace/新目录/笔记.txt"
    (tmp_path / "escape").symlink_to("/etc")
    with pytest.raises(OSError):
        workspace.read("escape/passwd")
    with pytest.raises(WorkspaceError, match="outside"):
        workspace.write("../escape", b"no")
    source = tmp_path / "新目录/笔记.txt"
    os.link(source, tmp_path / "alias")
    with pytest.raises(WorkspaceError, match="unsafe"):
        workspace.read("alias")


@pytest.mark.skipif(os.name != "posix", reason="POSIX host file operations")
def test_search_pagination_and_unicode_byte_cursor(tmp_path: Path):
    files = FileWorkspace(tmp_path)
    for index in range(125):
        (tmp_path / f"{index:03}.txt").write_text("found" if index == 124 else "skip")
    assert files.search("found")["matches"][0]["path"] == "/workspace/124.txt"
    content = ("字" * 11000).encode()
    metadata = files.write("中文.txt", content)
    first = files.read("中文.txt")
    second = files.read("中文.txt", offset=first["next_offset"])
    assert first["text"] + second["text"] == content.decode()
    assert metadata["version"] == hashlib.sha256(content).hexdigest()


@pytest.mark.skipif(os.name != "posix", reason="POSIX migration and confinement")
def test_migration_preserves_ids_collisions_and_reserved_names(tmp_path: Path):
    from qq_ai_bot.sandbox.persistent import PersistentManager

    store = WorkspaceStore(tmp_path / "artifacts")
    originals = [
        store.write(name, data)
        for name, data in [
            ("same.txt", b"one"),
            ("same.txt", b"two"),
            ("manifest.json", b"original manifest"),
        ]
    ]
    manager = PersistentManager(
        tmp_path / "manager", store, "test", "internal", "proxy", tmp_path / "home", testing=True
    )
    assert manager.migrate_files() == {"migrated": 3, "verified": True}
    assert manager.migrate_files() == {"migrated": 3, "verified": True}
    records = json.loads(manager.files.read("manifest.json")["text"])
    assert len({item["path"] for item in records}) == 3
    for item in originals:
        mapped = next(record for record in records if record["artifact_id"] == item["artifact_id"])
        assert manager.files.read(mapped["path"])["version"] == item["sha256"]
        assert store.read(item["artifact_id"])["sha256"] == item["sha256"]
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "file.txt").write_text("must not follow ancestor")
    alias = tmp_path / "home/link"
    alias.symlink_to(outside, target_is_directory=True)
    with pytest.raises(OSError):
        FileWorkspace(alias).read("file.txt")
    manager.db.close()
