"""Actual attachment checkout size limits and the offline bootstrap exclusion lock."""

import hashlib
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from qq_ai_bot.workspace.service import WorkspaceService
from qq_ai_bot.workspace.store import WorkspaceError, WorkspaceStore


@pytest.mark.skipif(os.name != "posix", reason="real checkout uses POSIX directory FDs")
@pytest.mark.parametrize(
    "size", [0, 4 << 20, (4 << 20) + 1, 200 << 20], ids=["empty", "4MiB", "4MiB+1", "200MiB"]
)
async def test_attachment_boundary_imports_real_snapshot_and_checkout(tmp_path, size):
    from qq_ai_bot.sandbox.persistent import PersistentManager

    source = tmp_path / "original"
    digest = hashlib.sha256()
    with source.open("wb") as stream:
        remaining = size
        while remaining:
            chunk = b"x" * min(65536, remaining)
            stream.write(chunk)
            digest.update(chunk)
            remaining -= len(chunk)
    store = WorkspaceStore(tmp_path / "artifacts")
    manager = PersistentManager(
        tmp_path / "manager", store, "test", "internal", "proxy", tmp_path / "home", testing=True
    )
    manager.files.root.mkdir(parents=True, exist_ok=True)
    service = WorkspaceService(store)
    service.sandbox = SimpleNamespace(execute=manager.file_request)
    service.conversation_media = SimpleNamespace(
        authorized_path=AsyncMock(return_value=(SimpleNamespace(), source))
    )
    runtime = SimpleNamespace(
        effective_conversation_id="conversation", turn_snapshot=None, gateway=None
    )
    args = {"event_id": 1, "attachment_index": 0, "destination": "same.bin"}
    try:
        first = await service.execute(
            "save_conversation_attachment_to_workspace",
            args,
            runtime=runtime,
            request_id="original-operation",
        )
        assert first["file_imported"] and first["size"] == size
        assert first["sha256"] == digest.hexdigest()
        with manager.files.open_file(first["path"]) as fd:
            version, actual_size = manager.files.fingerprint(fd)
        assert (version, actual_size.st_size) == (digest.hexdigest(), size)
        live = manager.files.write(first["path"], b"terminal edit", expected_version=version)
        replay = await service.execute(
            "save_conversation_attachment_to_workspace",
            args,
            runtime=runtime,
            request_id="original-operation",
        )
        assert replay == first
        assert manager.files.read(first["path"])["text"] == "terminal edit"
        assert store.read_bytes(first["artifact_id"])[0]["sha256"] == digest.hexdigest()
        assert live["version"] != first["version"]
        service.sandbox = None
        assert store.read(first["artifact_id"])["size"] == size
    finally:
        manager.db.close()


def test_snapshot_stream_failure_leaves_no_receipt_or_blob(tmp_path, monkeypatch):
    store = WorkspaceStore(tmp_path / "artifacts")
    source = tmp_path / "source"
    source.write_bytes(b"x" * 131072)
    original_read = os.read
    calls = 0

    def broken(fd, count):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("stream interrupted")
        return original_read(fd, count)

    monkeypatch.setattr(os, "read", broken)
    with source.open("rb") as stream, pytest.raises(OSError, match="stream interrupted"):
        store.snapshot(stream.fileno(), "file.bin")
    assert not store.list()["items"]
    assert not list(store.root.glob("*.pending"))


def test_snapshot_rejects_above_200_mib_without_publishing(tmp_path):
    store = WorkspaceStore(tmp_path / "artifacts")
    source = tmp_path / "large"
    with source.open("wb") as stream:
        stream.truncate((200 << 20) + 1)
    with source.open("rb") as stream, pytest.raises(WorkspaceError, match="artifact_too_large"):
        store.snapshot(stream.fileno(), "too-large.bin")
    assert not store.list()["items"]


@pytest.mark.skipif(os.name != "posix", reason="actual Manager checkout uses POSIX FDs")
async def test_checkout_lost_reply_recovers_original_receipt_and_rechecks_privacy(tmp_path):
    from qq_ai_bot.conversation.media_service import ConversationMediaError
    from qq_ai_bot.sandbox.persistent import PersistentManager

    source = tmp_path / "source"
    source.write_bytes(b"original attachment")
    store = WorkspaceStore(tmp_path / "artifacts")
    manager = PersistentManager(
        tmp_path / "manager", store, "test", "internal", "proxy", tmp_path / "home", testing=True
    )
    manager.files.root.mkdir(parents=True, exist_ok=True)
    service = WorkspaceService(store)
    media = AsyncMock(return_value=(SimpleNamespace(), source))
    service.conversation_media = SimpleNamespace(authorized_path=media)
    runtime = SimpleNamespace(
        effective_conversation_id="conversation", turn_snapshot=None, gateway=None
    )
    args = {"event_id": 1, "attachment_index": 0, "destination": "attachment.txt"}
    first = True

    async def lose_reply(method, arguments, *, request_id):
        nonlocal first
        reply = await manager.file_request(method, arguments, request_id)
        if first:
            first = False
            raise OSError("reply lost after checkout committed")
        return reply

    service.sandbox = SimpleNamespace(execute=lose_reply)
    try:
        with pytest.raises(OSError, match="reply lost"):
            await service.execute(
                "save_conversation_attachment_to_workspace",
                args,
                runtime=runtime,
                request_id="original",
            )
        original = manager.files.read("attachment.txt")
        manager.files.write(
            "attachment.txt", b"later terminal edit", expected_version=original["version"]
        )
        restored = await service.execute(
            "save_conversation_attachment_to_workspace",
            args,
            runtime=runtime,
            request_id="original",
        )
        assert restored["file_imported"] and restored["version"] == original["version"]
        assert manager.files.read("attachment.txt")["text"] == "later terminal edit"
        assert len(store.list()["items"]) == 1
        media.side_effect = ConversationMediaError("attachment_access_revoked")
        with pytest.raises(WorkspaceError, match="attachment_access_revoked"):
            await service.execute(
                "save_conversation_attachment_to_workspace",
                args,
                runtime=runtime,
                request_id="original",
            )
        assert len(store.list()["items"]) == 1
    finally:
        manager.db.close()


async def test_setup_bootstrap_uses_original_lock_and_active_bot_never_calls_offline_writer(
    tmp_path, monkeypatch
):
    from contextlib import asynccontextmanager

    from tests.conftest import make_settings

    from qq_ai_bot.deployment_setup.service import SetupPaths, apply_pending_plugins
    from qq_ai_bot.persistence.instance_lock import SQLiteApplicationLock

    settings = make_settings(f"sqlite+aiosqlite:///{tmp_path / 'bot.sqlite3'}")
    paths = SetupPaths(tmp_path)
    paths.pending.parent.mkdir(parents=True)
    paths.pending.write_text('{"schema_version":1,"selected_plugins":[]}', encoding="utf-8")
    offline = AsyncMock(return_value=7)
    monkeypatch.setattr("qq_ai_bot.deployment_setup.service._bootstrap_pending_plugins", offline)
    assert await apply_pending_plugins(paths, settings) == 7
    offline.assert_awaited_once()
    offline.reset_mock()

    @asynccontextmanager
    async def unavailable(_settings):
        raise RuntimeError("online control unavailable")
        yield

    monkeypatch.setattr("qq_ai_bot.plugin_host.control_client.plugin_control", unavailable)
    active = SQLiteApplicationLock(settings.sqlite_path)
    active.acquire()
    try:
        with pytest.raises(RuntimeError, match="online control unavailable"):
            await apply_pending_plugins(paths, settings)
        offline.assert_not_awaited()
        assert paths.pending.exists()
    finally:
        active.release()


async def test_first_offline_bootstrap_runs_without_control_or_bot(database, tmp_path, monkeypatch):
    from tests.conftest import make_settings

    from qq_ai_bot.deployment_setup.service import SetupPaths, apply_pending_plugins

    paths = SetupPaths(tmp_path)
    paths.pending.parent.mkdir(parents=True)
    paths.pending.write_text('{"schema_version":1,"selected_plugins":[]}', encoding="utf-8")
    settings = make_settings(database.url, plugin_directory=tmp_path / "plugins")
    monkeypatch.delenv("YUKI_CONTROL_CREDENTIAL", raising=False)
    assert await apply_pending_plugins(paths, settings) == 0
    assert not paths.pending.exists()


@pytest.mark.skipif(os.name != "posix", reason="persistent manager uses POSIX workspace")
async def test_retired_python_original_terminal_receipt_is_read_without_dispatch(tmp_path):
    import json
    from uuid import uuid4

    from qq_ai_bot.sandbox.persistent import PersistentManager

    store = WorkspaceStore(tmp_path / "artifacts")
    manager = PersistentManager(
        tmp_path / "manager", store, "test", "internal", "proxy", tmp_path / "home", testing=True
    )
    identity = str(uuid4())
    original = {"run_id": identity, "status": "succeeded", "stdout": "historical", "exit_code": 0}
    try:
        with manager.db:
            manager.db.execute(
                "INSERT INTO jobs VALUES (?,?,?,?,?,?,?)",
                (
                    identity,
                    "old-request",
                    "old-hash",
                    '{"kind":"run_python","code":"old"}',
                    "succeeded",
                    json.dumps(original),
                    1,
                ),
            )
            manager.db.execute(
                "INSERT INTO environment_jobs VALUES (?,?,?,?,?)",
                (identity, "run_python", None, None, 1),
            )
        manager.launch = AsyncMock(side_effect=AssertionError("historical run must not launch"))
        by_request = await manager.handle(
            {"method": "get_code_run_by_request", "args": {"request_id": "old-request"}}
        )
        by_run = await manager.handle({"method": "get_code_run", "args": {"run_id": identity}})
        assert by_request == by_run
        assert all(by_run[key] == value for key, value in original.items())
        assert by_run["pending"] is False
        retired = await manager.handle(
            {"method": "run_python", "args": {"code": "old"}, "request_id": "old-request"}
        )
        assert retired["error"] == "unknown_method"
        assert manager.db.execute("SELECT count(*) FROM jobs").fetchone()[0] == 1
        assert manager.queue.empty()
        manager.launch.assert_not_awaited()
    finally:
        manager.db.close()
