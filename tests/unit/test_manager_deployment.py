"""environment-check tests a live Manager, not a socket file."""

from __future__ import annotations

import asyncio
import json
import socket
import sys

import pytest

from yuki.sandbox.deployment_check import check_environment


async def test_stale_socket_file_does_not_pass_manager_deployment_check(tmp_path, monkeypatch):
    monkeypatch.setattr("yuki.sandbox.deployment_check.os.geteuid", lambda: 10001, raising=False)
    path = tmp_path / "manager.sock"
    if hasattr(socket, "AF_UNIX"):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stale:
            stale.bind(str(path))
    else:
        path.touch()
    assert path.exists()
    (tmp_path / ".artifact.lock").touch()
    result = await check_environment(path, tmp_path)
    assert result == {"ok": False, "error": "manager_connection_failed"}


@pytest.mark.skipif(sys.platform == "win32", reason="Unix socket server needs Linux")
async def test_live_manager_status_is_read_without_starting_a_job(tmp_path, monkeypatch):
    monkeypatch.setattr("yuki.sandbox.deployment_check.os.geteuid", lambda: 10001, raising=False)
    received = []

    async def respond(reader, writer):
        received.append(json.loads(await reader.readline()))
        writer.write(
            json.dumps(
                {"ready": True, "workspace": "/workspace", "private_task": "not-public"}
            ).encode()
            + b"\n"
        )
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    path = tmp_path / "manager.sock"
    (tmp_path / ".artifact.lock").touch()
    async with await asyncio.start_unix_server(respond, str(path)):
        result = await check_environment(path, tmp_path)
    assert result == {"ok": True, "uid": 10001, "manager": "connected", "workspace": "/workspace"}
    assert len(received) == 1 and received[0]["method"] == "environment_status"
