"""Publishers share one OS lock. Cleanup skips pending files while any of them copy."""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import textwrap
import threading
from pathlib import Path

import pytest

from yuki.workspace.artifact_lock import ArtifactRootLock
from yuki.workspace.store import WorkspaceError, WorkspaceStore

_SRC = Path(__file__).resolve().parents[2] / "src"
_CHILD = textwrap.dedent(
    """
    import sys
    import time
    from pathlib import Path

    sys.path.insert(0, sys.argv[1])
    from yuki.workspace.artifact_lock import ArtifactRootLock

    root = Path(sys.argv[2])
    lock = ArtifactRootLock(root)
    held = lock.acquire(exclusive=False)
    pending = root / sys.argv[3]
    pending.write_bytes(b"parallel-copy")
    print("copying", flush=True)
    if sys.argv[4] == "hold":
        sys.stdin.readline()
    else:
        time.sleep(30)
    lock.release(held)
    """
)


def _open(path: Path) -> int:
    return os.open(path, os.O_RDONLY)


def _child(root: Path, name: str, mode: str) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, "-c", _CHILD, str(_SRC), str(root), name, mode],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )


def test_lock_file_stays_empty_and_is_not_replaced(tmp_path):
    root = tmp_path / "artifacts"
    lock = ArtifactRootLock(root)
    first = lock.acquire(exclusive=False)
    second = lock.acquire(exclusive=False)
    assert lock.path.is_file()
    assert lock.path.stat().st_size == 0
    assert lock.path.read_bytes() == b""
    lock.release(second)
    lock.release(first)
    assert lock.path.read_bytes() == b""
    exclusive = lock.acquire(exclusive=True, wait=False)
    lock.release(exclusive)


def test_two_threads_copy_through_the_same_snapshot(tmp_path, monkeypatch):
    store = WorkspaceStore(tmp_path / "artifacts")
    left = tmp_path / "left.bin"
    right = tmp_path / "right.bin"
    left.write_bytes(b"LEFT-PUBLISHER-COPY-MARKER")
    right.write_bytes(b"RIGHT-PUBLISHER-COPY-MARKER")
    markers = {left.read_bytes(), right.read_bytes()}
    original = os.read
    overlap = threading.Barrier(2, timeout=3)

    def read(descriptor: int, size: int) -> bytes:
        data = original(descriptor, size)
        if data in markers:
            overlap.wait()
        return data

    monkeypatch.setattr(os, "read", read)
    errors: list[BaseException] = []

    def publish(path: Path, name: str) -> None:
        descriptor = _open(path)
        try:
            store.snapshot(descriptor, name)
        except BaseException as exc:
            errors.append(exc)
        finally:
            os.close(descriptor)

    threads = [
        threading.Thread(target=publish, args=(left, "left.bin")),
        threading.Thread(target=publish, args=(right, "right.bin")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)
        assert not thread.is_alive()
    assert errors == []
    assert len(list(store.root.glob("*.blob"))) == 2
    assert list(store.root.glob("*.pending")) == []


def test_processes_copy_together_and_a_third_process_cannot_delete_them(tmp_path):
    root = tmp_path / "artifacts"
    WorkspaceStore(root)
    first = _child(root, "one.pending", "hold")
    second = _child(root, "two.pending", "hold")
    try:
        assert first.stdout is not None and second.stdout is not None
        assert first.stdout.readline().strip() == "copying"
        assert second.stdout.readline().strip() == "copying"
        assert (root / "one.pending").is_file() and (root / "two.pending").is_file()
        cleaner = subprocess.Popen(
            [
                sys.executable,
                "-c",
                (
                    "import sys; from pathlib import Path; "
                    "from yuki.workspace.store import WorkspaceStore; "
                    "WorkspaceStore(Path(sys.argv[1])).cleanup()"
                ),
                str(root),
            ],
            env={**os.environ, "PYTHONPATH": str(_SRC)},
        )
        assert cleaner.wait(5) == 0
        assert (root / "one.pending").is_file() and (root / "two.pending").is_file()
    finally:
        for child in (first, second):
            if child.poll() is None and child.stdin is not None:
                child.stdin.write("\n")
                child.stdin.flush()
            child.wait(5)
    WorkspaceStore(root).cleanup()
    assert not (root / "one.pending").exists()
    assert not (root / "two.pending").exists()


def test_same_process_fd_and_killed_publisher_follow_the_lock(tmp_path):
    root = tmp_path / "artifacts"
    store = WorkspaceStore(root)
    held = ArtifactRootLock(root).acquire(exclusive=False)
    active = root / "active.pending"
    active.write_bytes(b"active")
    store.cleanup()
    assert active.is_file()
    ArtifactRootLock(root).release(held)
    store.cleanup()
    assert not active.exists()

    child = _child(root, "killed.pending", "sleep")
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "copying"
        assert (root / "killed.pending").is_file()
        child.kill()
        child.wait(5)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(5)
    store.cleanup()
    assert not (root / "killed.pending").exists()


def test_failed_publish_releases_the_lock_and_keeps_a_renamed_blob(tmp_path, monkeypatch):
    store = WorkspaceStore(tmp_path / "artifacts")
    source = tmp_path / "source.bin"
    source.write_bytes(b"published-bytes")
    digest = hashlib.sha256(b"published-bytes").hexdigest()
    real_replace = os.replace

    def replace_then_fail(source_path, destination) -> None:
        real_replace(source_path, destination)
        raise WorkspaceError("commit_unknown")

    monkeypatch.setattr("yuki.workspace.store.os.replace", replace_then_fail)
    descriptor = _open(source)
    try:
        with pytest.raises(WorkspaceError, match="commit_unknown"):
            store.snapshot(descriptor, "source.bin", expected_version=digest)
    finally:
        os.close(descriptor)
    blobs = list(store.root.glob("*.blob"))
    assert len(blobs) == 1
    assert blobs[0].read_bytes() == b"published-bytes"
    assert list(store.root.glob("*.pending")) == []
    assert store.root.joinpath(".artifact.lock").stat().st_size == 0
    exclusive = store._files.acquire(exclusive=True, wait=False)
    store._files.release(exclusive)
