from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from websockets.asyncio.server import serve

from yuki_agent_connector.config import Config, Instance
from yuki_agent_connector.journal import Journal


def runtime() -> tuple[str, dict[str, str]]:
    environment = dict(os.environ)
    if sys.platform != "win32":
        return sys.executable, environment
    # Exercise the actual windowless interpreter, not a venv console redirector.
    executable = Path(sys._base_executable).with_name("pythonw.exe")
    environment["PYTHONPATH"] = os.pathsep.join(
        [
            str(Path(__file__).resolve().parents[1] / "src"),
            str(Path(sys.prefix) / "Lib" / "site-packages"),
        ]
    )
    return str(executable), environment


@pytest.mark.parametrize("abrupt_writer", [False, True])
async def test_desktop_stdin_eof_stops_real_connector_and_preserves_native_owner(
    tmp_path: Path, abrupt_writer: bool
) -> None:
    hello, owner_connected = asyncio.Event(), asyncio.Event()
    owner_calls: list[dict[str, object]] = []
    owner_sockets = []
    received = []

    async def owner(socket):
        owner_sockets.append(socket)
        owner_connected.set()
        async for raw in socket:
            call = json.loads(raw)
            owner_calls.append(call)
            if "id" in call:
                await socket.send(json.dumps({"id": call["id"], "result": {}}))

    async def server(socket):
        async for raw in socket:
            message = json.loads(raw)
            received.append(message)
            if message["type"] == "hello":
                hello.set()

    async with serve(owner, "127.0.0.1", 0) as native, serve(server, "127.0.0.1", 0) as cloud:
        config = Config(
            f"ws://127.0.0.1:{cloud.sockets[0].getsockname()[1]}/agents/v1/connect",
            "original-device",
            "original-fixture-token",
            [
                Instance(
                    "original-instance",
                    "codex",
                    "Original Codex",
                    "not-started",
                    endpoint=f"ws://127.0.0.1:{native.sockets[0].getsockname()[1]}",
                )
            ],
        )
        path = tmp_path / "config.json"
        config.save(path)
        original_config = path.read_bytes()
        journal = Journal(tmp_path / "journal.sqlite3", namespace="original-device")
        journal.begin(
            {
                "id": "original-operation",
                "instance_id": "retired-instance",
                "action": "watch",
                "execution_id": "original-execution",
            }
        )
        journal.complete(
            "original-operation",
            {
                "result": {
                    "pending": True,
                    "status": "unknown",
                    "execution_id": "original-execution",
                }
            },
        )
        original_receipt = journal.operation("original-operation")
        journal.close()
        executable, environment = runtime()
        writer = None
        if abrupt_writer:
            writer = await asyncio.to_thread(
                subprocess.Popen,
                [sys.executable, "-c", "import sys; sys.stdin.buffer.read()"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            )
        process = await asyncio.create_subprocess_exec(
            executable,
            "-B",
            "-m",
            "yuki_agent_connector",
            "--data-dir",
            str(tmp_path),
            "run",
            "--desktop-stdin",
            env=environment,
            stdin=writer.stdout if writer else asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        if writer:
            assert writer.stdout is not None
            writer.stdout.close()
        try:
            await asyncio.wait_for(hello.wait(), 5)
            await asyncio.wait_for(owner_connected.wait(), 5)
            assert process.returncode is None
            if writer:
                writer.terminate()
                await asyncio.to_thread(writer.wait)
            else:
                assert process.stdin is not None
                process.stdin.close()
                await process.stdin.wait_closed()
            stdout, stderr = await asyncio.wait_for(process.communicate(), 5)
            assert process.returncode == 0, (stdout, stderr)
            assert path.read_bytes() == original_config
            assert received[0]["device_id"] == "original-device"
            assert received[0]["instances"][0]["id"] == "original-instance"
            assert not any(call.get("method") == "turn/start" for call in owner_calls)
            # The original native owner is still accepting clients after EOF.
            from websockets.asyncio.client import connect

            async with connect(
                f"ws://127.0.0.1:{native.sockets[0].getsockname()[1]}"
            ) as remaining_owner:
                await remaining_owner.send(json.dumps({"id": "still-owned", "method": "ping"}))
                assert json.loads(await remaining_owner.recv())["id"] == "still-owned"
            journal = Journal(tmp_path / "journal.sqlite3", namespace="original-device")
            try:
                assert journal.operation("original-operation") == original_receipt
                assert journal.db.execute("SELECT COUNT(*) FROM operations").fetchone()[0] == 1
            finally:
                journal.close()
            (tmp_path / "desktop-stdin-artifact.json").write_text(
                json.dumps(
                    {
                        "abrupt_writer": abrupt_writer,
                        "exit": process.returncode,
                        "device_id": received[0]["device_id"],
                        "instance_id": received[0]["instances"][0]["id"],
                        "original_owner_still_accepts_clients": True,
                        "config_unchanged": True,
                        "model_turn_requests": 0,
                        "original_unknown_receipt_preserved": True,
                    }
                ),
                encoding="utf-8",
            )
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
            if writer:
                if writer.poll() is None:
                    writer.kill()
                    writer.wait()
                assert writer.stdin is not None
                writer.stdin.close()


async def test_connector_failure_exits_while_desktop_stdin_is_still_open(tmp_path: Path) -> None:
    async def server(socket):
        await socket.recv()
        await socket.send("invalid original server payload")
        await socket.wait_closed()

    async with serve(server, "127.0.0.1", 0) as cloud:
        Config(
            f"ws://127.0.0.1:{cloud.sockets[0].getsockname()[1]}/agents/v1/connect",
            "original-device",
            "fixture",
        ).save(tmp_path / "config.json")
        executable, environment = runtime()
        process = await asyncio.create_subprocess_exec(
            executable,
            "-B",
            "-m",
            "yuki_agent_connector",
            "--data-dir",
            str(tmp_path),
            "run",
            "--desktop-stdin",
            env=environment,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        try:
            assert process.stdin is not None and not process.stdin.is_closing()
            await asyncio.wait_for(process.wait(), 5)
            stdout, stderr = await process.communicate()
            (tmp_path / "desktop-error-artifact.json").write_text(
                json.dumps(
                    {
                        "exit": process.returncode,
                        "stdin_still_owned_at_exit": True,
                        "stdout": stdout.decode(),
                        "stderr": stderr.decode(),
                    }
                ),
                encoding="utf-8",
            )
            assert process.returncode == 1, (stdout.decode(), stderr.decode())
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()


async def test_plain_run_keeps_serving_after_stdin_eof(tmp_path: Path) -> None:
    responded, fail = asyncio.Event(), asyncio.Event()
    responses = []

    async def server(socket):
        await socket.recv()
        await socket.send(
            json.dumps(
                {
                    "type": "command",
                    "id": "original-status",
                    "resource": "terminal",
                    "action": "status",
                    "arguments": {},
                }
            )
        )
        async for raw in socket:
            message = json.loads(raw)
            if message["type"] == "response":
                responses.append(message)
                responded.set()
                await fail.wait()
                await socket.send("original failure after observing stdin-independent work")
                await socket.wait_closed()
                return

    async with serve(server, "127.0.0.1", 0) as cloud:
        Config(
            f"ws://127.0.0.1:{cloud.sockets[0].getsockname()[1]}/agents/v1/connect",
            "original-device",
            "fixture",
        ).save(tmp_path / "config.json")
        executable, environment = runtime()
        process = await asyncio.create_subprocess_exec(
            executable,
            "-B",
            "-m",
            "yuki_agent_connector",
            "--data-dir",
            str(tmp_path),
            "run",
            env=environment,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        try:
            await asyncio.wait_for(responded.wait(), 5)
            assert process.returncode is None
            assert responses[0]["id"] == "original-status"
            fail.set()
            stdout, stderr = await asyncio.wait_for(process.communicate(), 5)
            assert process.returncode == 1, (stdout, stderr)
            (tmp_path / "plain-run-artifact.json").write_text(
                json.dumps(
                    {
                        "stdin_at_eof": True,
                        "original_status_response": responses[0],
                        "exit_after_real_protocol_failure": process.returncode,
                    }
                ),
                encoding="utf-8",
            )
        finally:
            fail.set()
            if process.returncode is None:
                process.kill()
                await process.wait()
