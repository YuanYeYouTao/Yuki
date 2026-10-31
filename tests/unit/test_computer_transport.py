"""Real Unix sockets, binary subprocess pipes and safe mutable-file reads."""

import asyncio
import hashlib
import json
import os
import sys
import tempfile
import threading
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from tests.unit.test_admin_api import admin as admin_fixture
from tests.unit.test_app_messages import app_env as app_fixture
from tests.unit.test_client_api import bearer
from tests.unit.test_client_works import owner
from tests.unit.test_computer_control import manager_at

from yuki.character.repository import CharacterRepository
from yuki.client_api.computer import ClientComputer
from yuki.client_api.http import ClientApi, attach_client_api
from yuki.identity.db_models import CanonicalPersonModel
from yuki.sandbox.client import SandboxClient
from yuki.workspace.store import WorkspaceError

admin = admin_fixture
app_env = app_fixture


async def test_http_file_etag_identifies_the_original_snapshot_after_source_changes(
    app_env, database, tmp_path, monkeypatch
):
    device, _person = await owner(app_env, database)
    async with database.immediate_session() as session:
        person = await session.get(CanonicalPersonModel, _person)
        person.is_superuser = True
    character = CharacterRepository(app_env.identity, app_env.harness.settings)
    await character.initialize("Computer snapshot test")
    manager = manager_at(tmp_path)
    manager.files.root.mkdir()
    source = manager.files.root / "中文快照.bin"
    original = "原始快照\n".encode() * 256 + b"\0"
    replacement = b"later mutable source"
    source.write_bytes(original)
    snapshot = manager.computer_transport.file_snapshot

    def capture_then_change(path):
        captured = snapshot(path)
        source.write_bytes(replacement)
        return captured

    monkeypatch.setattr(manager.computer_transport, "file_snapshot", capture_then_change)
    short = tempfile.TemporaryDirectory(prefix="yc-http-", dir="/tmp")
    socket = Path(short.name) / "manager.sock"
    server = await asyncio.start_unix_server(manager.serve, str(socket))
    computer = ClientComputer(app_env.identity, SandboxClient(socket))
    app = FastAPI()
    attach_client_api(app, lambda: ClientApi(app_env.identity, computer=computer))
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://computer"
        ) as client:
            response = await client.get(
                "/client/v1/computer/files/content",
                params={"path": source.name},
                headers=bearer(device["token"]),
            )
            assert response.status_code == 200, response.text
            assert source.read_bytes() == replacement
            assert response.content == original
            digest = hashlib.sha256(original).hexdigest()
            assert response.headers.get("etag") == f'"{digest}"'
            assert response.headers["x-yuki-sha256"] == digest
            assert response.headers["content-length"] == str(len(original))
            assert response.headers["cache-control"] == "no-store"
            next_response = await client.get(
                "/client/v1/computer/files/content",
                params={"path": source.name},
                headers=bearer(device["token"]),
            )
            assert next_response.status_code == 200 and next_response.content == replacement
            assert next_response.headers["etag"] == (f'"{hashlib.sha256(replacement).hexdigest()}"')
            assert next_response.headers["etag"] != response.headers["etag"]
    finally:
        await computer.close()
        server.close()
        await server.wait_closed()
        short.cleanup()
        await manager.close()


async def test_mutable_file_stream_keeps_read_snapshot_hash_after_source_changes(tmp_path):
    manager = manager_at(tmp_path)
    manager.files.root.mkdir()
    data = b"original current file bytes\0" * 100000
    source = manager.files.root / "mutable.bin"
    source.write_bytes(data)
    short = tempfile.TemporaryDirectory(prefix="yc-", dir="/tmp")
    socket = Path(short.name) / "m.sock"
    server = await asyncio.start_unix_server(manager.serve, str(socket))
    try:
        reader, writer = await asyncio.open_unix_connection(str(socket))
        writer.write(b'{"method":"files_read","args":{"path":"mutable.bin"}}\n')
        await writer.drain()
        metadata = json.loads(await asyncio.wait_for(reader.readline(), 5))
        source.write_bytes(b"later source")
        body = await asyncio.wait_for(reader.read(), 5)
        assert body == data and metadata["sha256"] == hashlib.sha256(body).hexdigest()
        assert metadata["size"] == len(data)
        writer.close()
        await writer.wait_closed()
        second, second_meta = manager.computer_transport.file_snapshot("mutable.bin")
        try:
            assert second.read() == b"later source" and second_meta["sha256"] != metadata["sha256"]
        finally:
            second.close()
    finally:
        server.close()
        await server.wait_closed()
        short.cleanup()
        await manager.close()


def test_files_page_is_scoped_to_workspace_and_symlink_targets_cannot_be_read(tmp_path):
    manager = manager_at(tmp_path)
    manager.files.root.mkdir()
    try:
        (manager.files.root / "中文.txt").write_text("current")
        (manager.files.root / "directory").mkdir()
        (manager.files.root / "escape").symlink_to(tmp_path / "outside")
        (tmp_path / "outside").write_text("host data")
        page = manager.computer_transport.files_list({"limit": 1})
        assert len(page["items"]) == 1 and page["next_after"]
        next_page = manager.computer_transport.files_list({"after": page["next_after"]})
        assert {item["name"] for item in page["items"] + next_page["items"]} == {
            "中文.txt",
            "directory",
            "escape",
        }
        with pytest.raises((OSError, WorkspaceError)):
            manager.computer_transport.file_snapshot("escape")
        with pytest.raises(WorkspaceError):
            manager.computer_transport.file_snapshot("../outside")
    finally:
        manager.db.close()


def test_large_file_pages_keep_the_requested_size_and_cursor(tmp_path):
    manager = manager_at(tmp_path)
    manager.files.root.mkdir()
    try:
        for index in range(105):
            (manager.files.root / f"entry-{index:03}.txt").write_bytes(b"actual workspace file")
        page = manager.computer_transport.files_list({"limit": 101})
        assert len(page["items"]) == 101
        tail = manager.computer_transport.files_list({"limit": 101, "after": page["next_after"]})
        assert len(tail["items"]) == 4 and tail["next_after"] is None
        names = [item["name"] for item in page["items"] + tail["items"]]
        assert names == [f"entry-{index:03}.txt" for index in range(105)]
    finally:
        manager.db.close()


async def test_cancelled_snapshot_preparation_closes_its_real_temporary_fd(tmp_path, monkeypatch):
    manager = manager_at(tmp_path)
    manager.files.root.mkdir()
    (manager.files.root / "file").write_bytes(b"actual")
    prepared, release = threading.Event(), threading.Event()
    original = manager.computer_transport.file_snapshot
    handles = []

    def held(path):
        snapshot, metadata = original(path)
        handles.append(snapshot)
        prepared.set()
        release.wait(5)
        return snapshot, metadata

    monkeypatch.setattr(manager.computer_transport, "file_snapshot", held)
    task = asyncio.create_task(manager.computer_transport.files_read("file", None))
    await asyncio.to_thread(prepared.wait, 5)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert handles and handles[0].closed
    await manager.close()


async def test_display_stream_uses_separate_container_exec_with_binary_stdio_and_ends_on_disconnect(
    tmp_path, monkeypatch
):
    manager = manager_at(tmp_path)
    binary = tmp_path / "bin"
    binary.mkdir()
    recorded = tmp_path / "exec.json"
    docker = binary / "docker"
    docker.write_text(f"""#!{sys.executable}
import json,os,sys
from pathlib import Path
Path(os.environ['CAPTURE']).write_text(json.dumps(sys.argv[1:]))
os.write(1,b'RFB 003.008\\n')
while True:
 data=os.read(0,65536)
 if not data:break
 os.write(1,data)
""")
    docker.chmod(0o755)
    monkeypatch.setenv("PATH", str(binary) + ":" + os.environ["PATH"])
    monkeypatch.setenv("CAPTURE", str(recorded))
    short = tempfile.TemporaryDirectory(prefix="yc-", dir="/tmp")
    socket = Path(short.name) / "m.sock"
    server = await asyncio.start_unix_server(manager.serve, str(socket))
    try:
        reader, writer = await asyncio.open_unix_connection(str(socket))
        writer.write(b'{"method":"display_stream","args":{}}\n')
        await writer.drain()
        assert json.loads(await asyncio.wait_for(reader.readline(), 3)) == {"ready": True}
        assert await reader.readexactly(12) == b"RFB 003.008\n"
        wire = bytes(range(256)) * 300
        writer.write(wire)
        await writer.drain()
        assert await asyncio.wait_for(reader.readexactly(len(wire)), 3) == wire
        assert json.loads(recorded.read_text()) == [
            "exec",
            "-i",
            "yuki-environment",
            "socat",
            "STDIO",
            "TCP:127.0.0.1:5901",
        ]
        writer.write_eof()
        assert await asyncio.wait_for(reader.read(), 3) == b""
        writer.close()
        await writer.wait_closed()
    finally:
        server.close()
        await server.wait_closed()
        short.cleanup()
        await manager.close()
