"""Private media reads hash their one FD pass and retain actual source checks."""

import base64
import hashlib
import os

import pytest

from yuki.sandbox.persistent import PersistentManager
from yuki.workspace.store import WorkspaceError, WorkspaceStore

pytestmark = pytest.mark.skipif(
    os.name != "posix", reason="Manager safe workspace FDs require POSIX"
)


@pytest.fixture
def manager(tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir()
    value = PersistentManager(
        tmp_path / "manager",
        WorkspaceStore(tmp_path / "artifacts"),
        "test-image",
        "test-network",
        "",
        tmp_path / "home",
        testing=True,
        media_cache=cache,
    )
    value.files.root.mkdir(parents=True)
    yield value
    value.db.close()


@pytest.mark.parametrize("shared", [False, True])
def test_media_read_returns_selected_original_bytes(manager, shared):
    content = b"actual original" * 10000
    source = manager.media_cache.root if shared else manager.files.root
    (source / "original.bin").write_bytes(content)
    path = "/inputs/media-cache/original.bin" if shared else "original.bin"
    version = hashlib.sha256(content).hexdigest()
    result = manager.file_operation(
        "workspace_media_read", {"path": path, "expected_version": version}
    )
    assert result["size"] == len(content) and result["version"] == version
    assert base64.b64decode(result["base64"]) == content


def test_media_read_rejects_source_changed_during_same_fd_read(manager, monkeypatch):
    source = manager.files.root / "changing.bin"
    content = b"a" * 140000
    source.write_bytes(content)
    original = os.read
    changed = False

    def change_after_first_chunk(fd, size):
        nonlocal changed
        data = original(fd, size)
        if data and not changed:
            changed = True
            source.write_bytes(b"b" * len(content))
            observed = source.stat()
            os.utime(source, ns=(observed.st_atime_ns, observed.st_mtime_ns + 1_000_000))
        return data

    monkeypatch.setattr(os, "read", change_after_first_chunk)
    with pytest.raises(WorkspaceError, match="file_changed_during_read"):
        manager.file_operation("workspace_media_read", {"path": "changing.bin"})
    assert changed


@pytest.mark.parametrize("method", ["workspace_media_read", "workspace_media_validate"])
def test_media_selected_version_conflict_is_not_skipped(manager, method):
    (manager.files.root / "changed.bin").write_bytes(b"current bytes")
    with pytest.raises(WorkspaceError, match="version_conflict"):
        manager.file_operation(
            method,
            {"path": "changed.bin", "expected_version": hashlib.sha256(b"old").hexdigest()},
        )


def test_media_read_rejects_actual_size_over_limit_before_reading(manager, monkeypatch):
    with (manager.files.root / "large.bin").open("wb") as stream:
        stream.truncate(20 * 1024 * 1024 + 1)

    def no_read(_fd, _size):
        raise AssertionError("an already oversized source must not be loaded")

    monkeypatch.setattr(os, "read", no_read)
    with pytest.raises(WorkspaceError, match="workspace_media_too_large"):
        manager.file_operation("workspace_media_read", {"path": "large.bin"})


def test_media_read_keeps_bound_if_source_grows_after_initial_stat(manager, monkeypatch):
    source = manager.files.root / "growing.bin"
    source.write_bytes(b"original")
    original = os.read
    grown = False
    limit = 20 * 1024 * 1024

    def grow_before_first_read(fd, size):
        nonlocal grown
        if not grown:
            grown = True
            with source.open("ab") as stream:
                stream.truncate(limit + 100)
        data = original(fd, size)
        return data

    monkeypatch.setattr(os, "read", grow_before_first_read)
    with pytest.raises(WorkspaceError, match="workspace_media_too_large"):
        manager.file_operation("workspace_media_read", {"path": "growing.bin"})
