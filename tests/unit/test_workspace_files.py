"""Ordinary targets overwrite normally; a copy validates the actual streamed source."""

import hashlib
import os

import pytest

from yuki.workspace.files import FileWorkspace
from yuki.workspace.store import WorkspaceError

pytestmark = pytest.mark.skipif(os.name != "posix", reason="Workspace FDs require POSIX")


def test_write_overwrites_without_target_cas(tmp_path):
    files = FileWorkspace(tmp_path)
    files.write("existing.txt", b"old target")
    result = files.write("existing.txt", b"new target")
    assert (tmp_path / "existing.txt").read_bytes() == b"new target"
    assert result["version"] == hashlib.sha256(b"new target").hexdigest()


def test_copy_streams_once_and_does_not_gate_on_target_edits(tmp_path, monkeypatch):
    files = FileWorkspace(tmp_path)
    content = b"selected bytes" * 18000
    source = tmp_path / "source.bin"
    source.write_bytes(content)
    target = tmp_path / "target.bin"
    target.write_bytes(b"old target")
    read = os.read
    reads = []
    with files.open_file("source.bin") as fd:

        def edit_target(source_fd, count):
            chunk = read(source_fd, count)
            if source_fd == fd:
                reads.append(len(chunk))
                if len(reads) == 1:
                    target.write_bytes(b"terminal edit while copy is in progress")
            return chunk

        monkeypatch.setattr(os, "read", edit_target)
        result = files.copy("target.bin", fd, expected_version=hashlib.sha256(content).hexdigest())
    assert target.read_bytes() == content
    assert result["size"] == sum(reads) == len(content)
    assert max(reads) <= 65536 and len(reads) > 2
    assert not list(tmp_path.glob(".yuki-write-*"))


@pytest.mark.parametrize("changed_during_read", [False, True])
def test_copy_rejects_changed_selected_source_without_replacing_target(
    tmp_path, monkeypatch, changed_during_read
):
    files = FileWorkspace(tmp_path)
    source = tmp_path / "source.bin"
    source.write_bytes(b"A" * 150000)
    target = tmp_path / "target.bin"
    target.write_bytes(b"existing user file")
    read = os.read
    with files.open_file("source.bin") as fd:
        changed = False

        def alter_source(source_fd, count):
            nonlocal changed
            chunk = read(source_fd, count)
            if source_fd == fd and changed_during_read and not changed:
                changed = True
                source.write_bytes(b"edited source")
            return chunk

        monkeypatch.setattr(os, "read", alter_source)
        with pytest.raises(
            WorkspaceError,
            match="file_changed_during_read" if changed_during_read else "version_conflict",
        ):
            files.copy(
                "target.bin", fd, expected_version=hashlib.sha256(b"other version").hexdigest()
            )
    assert target.read_bytes() == b"existing user file"
    assert not list(tmp_path.glob(".yuki-write-*"))


def test_copy_does_not_follow_target_parent_outside_workspace(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (workspace / "escape").symlink_to(outside, target_is_directory=True)
    files = FileWorkspace(workspace)
    files.write("source.txt", b"selected source")
    with files.open_file("source.txt") as fd, pytest.raises(OSError):
        files.copy("escape/target.txt", fd)
    assert list(outside.iterdir()) == []
