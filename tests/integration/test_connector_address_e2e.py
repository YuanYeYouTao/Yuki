"""Actual CLI, original journals and a live TLS Connector during address changes."""

import asyncio
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from dataclasses import asdict, replace
from pathlib import Path
from uuid import uuid4

import pytest
from tests.integration.test_external_agent_bridge import bridge as bridge
from tests.integration.test_local_device_transport import actual_connector, python_command
from yuki_agent_connector.config import Config, Instance
from yuki_agent_connector.journal import Journal

from yuki.sandbox.client import SandboxClient

CONNECTOR_SOURCE = Path(__file__).resolve().parents[2] / "packages/yuki-agent-connector/src"


@pytest.fixture
def address_evidence(request):
    parent = (
        request.config.rootpath
        / "test-results"
        / "connector-address"
        / hashlib.sha256(request.node.nodeid.encode()).hexdigest()[:16]
    )
    parent.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix="attempt-", dir=parent))
    facts = {}
    try:
        yield facts
    finally:
        (directory / "evidence.json").write_text(
            json.dumps(
                {
                    "nodeid": request.node.nodeid,
                    "revision": subprocess.check_output(
                        ["git", "rev-parse", "HEAD"], text=True
                    ).strip(),
                    "facts": facts,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )


async def cli_process(directory, *arguments):
    return await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "yuki_agent_connector",
        "--data-dir",
        str(directory),
        *arguments,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={
            **os.environ,
            "PYTHONPATH": str(CONNECTOR_SOURCE),
            "NO_PROXY": "127.0.0.1,localhost,::1",
        },
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )


@pytest.mark.parametrize(
    ("server", "endpoint"),
    [
        ("https://new.example/role/yuki/", "wss://new.example/role/yuki/agents/v1/connect"),
        ("http://127.0.0.1:18767/role/yuki", "ws://127.0.0.1:18767/role/yuki/agents/v1/connect"),
        ("http://localhost:18767", "ws://localhost:18767/agents/v1/connect"),
        ("http://[::1]:18767", "ws://[::1]:18767/agents/v1/connect"),
    ],
)
async def test_server_cli_only_saves_original_installation(
    tmp_path, server, endpoint, address_evidence
):
    directory = tmp_path / "original-installation"
    original = Config(
        "wss://old.example/agents/v1/connect",
        str(uuid4()),
        "ISOLATED_TOKEN_NEVER_PRINT",
        [
            Instance(
                "original-agent",
                "acp",
                "Original agent",
                "not-started-native-program",
                args=["--original-argument"],
                cwd=str(tmp_path),
                env={"ISOLATED_NATIVE_SECRET": "NEVER_PRINT"},
                endpoint="ws://original-native-owner.invalid",
            )
        ],
        full_access=True,
    )
    path = directory / "config.json"
    original.save(path)
    service = directory / "service.xml"
    service.write_bytes(b"original service registration, never installed by this test")
    process = await cli_process(directory, "server", "--url", server)
    stdout, stderr = await process.communicate()
    address_evidence.update(
        exit_code=process.returncode,
        stdout=stdout.decode(),
        stderr=stderr.decode(),
        device_id=original.device_id,
        expected_endpoint=endpoint,
        journal_created=(directory / "journal.sqlite3").exists(),
    )
    assert process.returncode == 0
    saved = Config.load(path)
    assert asdict(saved) == asdict(replace(original, url=endpoint))
    assert b"ISOLATED_TOKEN_NEVER_PRINT" not in stdout + stderr
    assert b"ISOLATED_NATIVE_SECRET" not in stdout + stderr
    assert service.read_bytes() == b"original service registration, never installed by this test"
    assert not (directory / "journal.sqlite3").exists()
    address_evidence["original_fields_preserved"] = True


@pytest.mark.parametrize("server", ["http://100.100.100.100", "http://remote.invalid", "ftp://host"])
async def test_server_cli_keeps_remote_tls_requirement(tmp_path, server, address_evidence):
    directory = tmp_path / "original-installation"
    path = directory / "config.json"
    Config("wss://old.example/agents/v1/connect", str(uuid4()), "ISOLATED_TOKEN").save(path)
    before = path.read_bytes()
    process = await cli_process(directory, "server", "--url", server)
    stdout, stderr = await process.communicate()
    address_evidence.update(exit_code=process.returncode, stderr=stderr.decode())
    assert process.returncode == 1
    assert path.read_bytes() == before
    assert b"ISOLATED_TOKEN" not in stdout + stderr


def journal_snapshot(path):
    with sqlite3.connect(path) as database:
        return {
            table: database.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
            for table in ("installation", "operations", "events")
        }


@pytest.mark.parametrize("receipt_version", [0, 1])
async def test_cli_start_upgrades_old_url_namespace_before_connecting(
    tmp_path, receipt_version, address_evidence
):
    accepted = asyncio.Event()

    async def hold_handshake(reader, writer):
        try:
            await reader.readuntil(b"\r\n\r\n")
            accepted.set()
            await reader.read()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(hold_handshake, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    directory = tmp_path / "original-installation"
    device_id = str(uuid4())
    path = directory / "journal.sqlite3"
    old_namespace = json.dumps(["wss://old.example/agents/v1/connect", device_id])
    journal = Journal(path, namespace=old_namespace)
    for identity, state in [("original-completed", "succeeded"), ("original-pending", "running")]:
        journal.begin(
            {"id": identity, "resource": "terminal", "action": "exec", "run_id": identity}
        )
        journal.complete(identity, {"result": {"status": state, "run_id": identity}})
    journal.append(
        {
            "type": "terminal_event",
            "event_id": "original-unconfirmed-event",
            "operation_id": "original-pending",
            "kind": "progress",
            "result": {"status": "running", "run_id": "original-pending"},
        }
    )
    if receipt_version == 0:
        journal.db.execute("UPDATE operations SET receipt=NULL")
    journal.db.execute(f"PRAGMA user_version={receipt_version}")
    journal.db.commit()
    journal.close()
    before = journal_snapshot(path)
    Config(f"ws://127.0.0.1:{port}/agents/v1/connect", device_id, "ISOLATED_TOKEN").save(
        directory / "config.json"
    )
    process = await cli_process(directory, "run")
    handshake = asyncio.create_task(accepted.wait())
    exited = asyncio.create_task(process.wait())
    try:
        async with asyncio.timeout(8):
            await asyncio.wait([handshake, exited], return_when=asyncio.FIRST_COMPLETED)
        after = journal_snapshot(path)
        address_evidence.update(
            device_id=device_id,
            receipt_version=receipt_version,
            connected=accepted.is_set(),
            exit_code=process.returncode,
            before=before,
            after=after,
        )
        assert accepted.is_set()
        assert after["installation"] == [(device_id,)]
        assert [row[0] for row in after["operations"]] == [
            "original-completed",
            "original-pending",
        ]
        assert json.loads(after["operations"][0][4])["status"] == "succeeded"
        assert json.loads(after["operations"][1][4])["status"] == "running"
        assert after["events"][0][1] == "original-unconfirmed-event"
        if receipt_version == 1:
            assert after["operations"] == before["operations"]
            assert after["events"] == before["events"]
    finally:
        if process.returncode is None:
            process.terminate()
        await process.communicate()
        handshake.cancel()
        await asyncio.gather(handshake, exited, return_exceptions=True)
        server.close()
        await server.wait_closed()
    reopened = Journal(path, namespace=device_id)
    reopened.close()
    assert journal_snapshot(path) == after
    address_evidence["same_device_reopen_preserved"] = True


async def test_cli_start_rejects_another_device_without_rewriting_original_evidence(
    tmp_path, address_evidence
):
    directory = tmp_path / "original-installation"
    path = directory / "journal.sqlite3"
    original_device = str(uuid4())
    namespace = json.dumps(["wss://old.example/agents/v1/connect", original_device])
    journal = Journal(path, namespace=namespace)
    journal.begin({"id": "original-operation", "action": "exec", "run_id": "original-run"})
    journal.close()
    before = journal_snapshot(path)
    other_device = str(uuid4())
    Config("wss://new.example/agents/v1/connect", other_device, "ISOLATED_TOKEN").save(
        directory / "config.json"
    )
    process = await cli_process(directory, "run")
    async with asyncio.timeout(8):
        stdout, stderr = await process.communicate()
    address_evidence.update(
        original_device_id=original_device,
        configured_device_id=other_device,
        exit_code=process.returncode,
        stderr=stderr.decode(),
    )
    assert process.returncode == 1
    assert journal_snapshot(path) == before
    assert b"ISOLATED_TOKEN" not in stdout + stderr
    address_evidence["original_evidence_preserved"] = True


async def test_running_connector_keeps_original_shell_when_address_is_saved(
    bridge, tmp_path, monkeypatch, address_evidence
):
    async with actual_connector(bridge, tmp_path, monkeypatch) as connector:
        path = tmp_path / "isolated-connector" / "config.json"
        connector.config.save(path)
        connector.config_path = path
        original_socket = connector.stream
        source = {
            **bridge.source,
            "conversation_id": bridge.env.context.conversation_id,
            "parent_execution_id": "original-address-execution",
        }
        client = SandboxClient(tmp_path / "unused-sandbox", tasks=bridge.service.tasks)
        client.local_devices = bridge.service.local_devices
        script = tmp_path / "original-shell.py"
        script.write_text(
            "input()\nprint('original-shell-completed', flush=True)\n", encoding="utf-8"
        )
        started = await client.execute(
            "terminal_exec",
            {
                "device_id": bridge.device["device_id"],
                "command": python_command(script),
                "cwd": str(tmp_path),
            },
            request_id="original-address-shell",
            source=source,
        )
        original_run = connector.terminal.runs[started["run_id"]]
        saved = replace(
            connector.config, url=connector.config.url.replace("127.0.0.1", "localhost")
        )
        saved.save(path)
        response = await client.execute(
            "terminal_control",
            {"run_id": started["run_id"], "action": "input", "text": "continue\n"},
            request_id="original-address-input",
            source=source,
        )
        address_evidence.update(
            device_id=bridge.device["device_id"],
            run_id=started["run_id"],
            original_pid=original_run.process.pid,
            response=response,
            original_socket_retained=connector.stream is original_socket,
            saved_endpoint=Config.load(path).url,
            running_endpoint=connector.config.url,
        )
        assert response.get("input_delivered") is True
        async with asyncio.timeout(8):
            await original_run.done.wait()
        assert original_run.process.returncode == 0
        assert connector.stream is original_socket
        assert connector.config.url != saved.url
        assert connector.journal.operation("original-address-shell")["run_id"] == started["run_id"]
