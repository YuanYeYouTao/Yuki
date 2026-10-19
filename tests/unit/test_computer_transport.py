"""Real Unix sockets, binary subprocess pipes and safe mutable-file reads."""

import asyncio
import hashlib
import json
import os
import sys
import tempfile
import threading
from pathlib import Path

import pytest
from tests.unit.test_computer_control import manager_at

from yuki.workspace.store import WorkspaceError


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
