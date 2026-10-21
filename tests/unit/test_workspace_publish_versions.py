"""Publication checks the bytes actually copied across the existing FD bridge."""

import asyncio
import hashlib
import os
import threading
from tempfile import TemporaryFile

import pytest

from yuki.sandbox.client import SandboxClient
from yuki.sandbox.persistent import PersistentManager
from yuki.workspace.store import WorkspaceError, WorkspaceStore


def test_snapshot_expected_version_rejects_actual_mismatch_before_artifact_write(tmp_path):
    store = WorkspaceStore(tmp_path / "artifacts")
    with TemporaryFile() as source:
        source.write(b"changed content")
        source.flush()
        with pytest.raises(WorkspaceError, match="version_conflict"):
            store.snapshot(
                source.fileno(), "selected.png", expected_version=hashlib.sha256(b"old").hexdigest()
            )
    assert store.list()["items"] == []
    assert list(store.root.glob("*.blob")) == list(store.root.glob("*.pending")) == []


@pytest.mark.skipif(os.name != "posix", reason="Manager safe workspace FDs require POSIX")
@pytest.mark.parametrize("expected", [False, True])
def test_manager_publish_uses_single_snapshot_digest_even_when_source_changes(
    tmp_path, monkeypatch, expected
):
    store = WorkspaceStore(tmp_path / "artifacts")
    manager = PersistentManager(
        tmp_path / "manager",
        store,
        "test-image",
        "test-network",
        "",
        tmp_path / "home",
        testing=True,
    )
    manager.files.root.mkdir(parents=True, exist_ok=True)
    name = "long-original-filename-" + "a" * 160 + ".png"
    source = manager.files.root / name
    source.write_bytes(b"original A")
    original = store.snapshot

    def copy_changed_file(*args, **kwargs):
        # Actual open POSIX FD remains the same inode; no fake copy/hash API.
        source.write_bytes(b"changed B")
        return original(*args, **kwargs)

    monkeypatch.setattr(store, "snapshot", copy_changed_file)
    arguments = {"path": name}
    if expected:
        arguments["expected_version"] = hashlib.sha256(b"original A").hexdigest()
    try:
        if expected:
            with pytest.raises(WorkspaceError, match="version_conflict"):
                manager.file_operation("workspace_publish", arguments)
            assert store.list()["items"] == []
        else:
            result = manager.file_operation("workspace_publish", arguments)
            metadata, content = store.read_bytes(result["artifact_id"])
            assert content == b"changed B" and metadata["name"] == name
            assert result["version"] == result["sha256"] == hashlib.sha256(content).hexdigest()
    finally:
        manager.db.close()


@pytest.mark.skipif(os.name != "posix", reason="Manager safe workspace FDs require POSIX")
async def test_actual_publish_rpc_waits_for_its_copy_instead_of_a_fixed_observation(
    tmp_path, monkeypatch
):
    store = WorkspaceStore(tmp_path / "artifacts")
    manager = PersistentManager(
        tmp_path / "manager",
        store,
        "test-image",
        "test-network",
        "",
        tmp_path / "home",
        testing=True,
    )
    manager.files.root.mkdir(parents=True, exist_ok=True)
    (manager.files.root / "selected.png").write_bytes(b"actual bytes after slow I/O")
    entered, release = threading.Event(), threading.Event()
    original = manager.file_operation
    handlers = set()

    def slow_copy(method, args):
        entered.set()
        release.wait()
        return original(method, args)

    async def accept(reader, writer):
        current = asyncio.current_task()
        handlers.add(current)
        try:
            await manager.serve(reader, writer)
        finally:
            handlers.discard(current)

    monkeypatch.setattr(manager, "file_operation", slow_copy)
    socket = tmp_path / "manager.sock"
    server = await asyncio.start_unix_server(accept, str(socket))
    task = asyncio.create_task(
        SandboxClient(socket).execute(
            "workspace_publish",
            {"path": "selected.png"},
            request_id="actual-copy",
        )
    )
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        await asyncio.sleep(7.05)
        assert not task.done(), "the actual file owner is still copying"
        release.set()
        result = await task
        assert store.read_bytes(result["artifact_id"])[1] == b"actual bytes after slow I/O"
    finally:
        release.set()
        await asyncio.gather(task, *tuple(handlers), return_exceptions=True)
        server.close()
        await server.wait_closed()
        await manager.close()


@pytest.mark.skipif(os.name != "posix", reason="Manager safe workspace FDs require POSIX")
async def test_slow_actual_copy_does_not_hold_other_requests_or_repeat_the_same_request(
    tmp_path, monkeypatch
):
    store = WorkspaceStore(tmp_path / "artifacts")
    manager = PersistentManager(
        tmp_path / "manager",
        store,
        "test-image",
        "test-network",
        "",
        tmp_path / "home",
        testing=True,
    )
    manager.files.root.mkdir(parents=True, exist_ok=True)
    (manager.files.root / "slow.txt").write_bytes(b"slow original")
    (manager.files.root / "fast.txt").write_bytes(b"independent original")
    entered, release = threading.Event(), threading.Event()
    original = manager.file_operation
    copied = []

    def blocked_copy(method, args):
        copied.append(args["path"])
        if args["path"] == "slow.txt":
            entered.set()
            release.wait()
        return original(method, args)

    monkeypatch.setattr(manager, "file_operation", blocked_copy)
    slow = asyncio.create_task(
        manager.file_request("workspace_publish", {"path": "slow.txt"}, "slow")
    )
    tasks = [slow]
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        duplicate = asyncio.create_task(
            manager.file_request("workspace_publish", {"path": "slow.txt"}, "slow")
        )
        fast = asyncio.create_task(
            manager.file_request("workspace_publish", {"path": "fast.txt"}, "fast")
        )
        tasks.extend((duplicate, fast))
        completed, _ = await asyncio.wait((fast,), timeout=3)
        assert completed == {fast}, "one real copy held another request's preparation"
        assert not duplicate.done(), "the same original copy is still in progress"
        result = fast.result()
        assert store.read_bytes(result["artifact_id"])[1] == b"independent original"
        assert copied.count("slow.txt") == 1
        release.set()
        original_result, replayed_result = await asyncio.gather(slow, duplicate)
        assert original_result == replayed_result and copied.count("slow.txt") == 1
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        manager.db.close()
