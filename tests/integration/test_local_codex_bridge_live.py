"""Opt-in real owner/model test; default suites never submit a paid native task."""

import asyncio
import hashlib
import json
import os
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select
from tests.integration.test_external_agent_bridge import accepted_work
from tests.integration.test_external_agent_bridge import bridge as bridge

from yuki.external_agents.db_models import AgentEventModel, AgentOperationModel, AgentSessionModel
from yuki.runtime.work_schema_v1 import inputs
from yuki.sandbox.continuation_worker import SandboxContinuationWorker
from yuki.sandbox.db_models import SandboxTaskRunModel

pytestmark = pytest.mark.skipif(
    os.environ.get("YUKI_REAL_CODEX_TEST") != "1",
    reason="Set YUKI_REAL_CODEX_TEST=1 and YUKI_CODEX_BINARY for an explicitly paid native test",
)


def isolated_command_approval(request, cwd, expected_commands):
    """Authorize only this test's exact commands, never an amended exec policy."""
    actions = request.get("commandActions")
    return (
        isinstance(request.get("cwd"), str)
        and Path(request["cwd"]).resolve() == cwd.resolve()
        and isinstance(actions, list)
        and len(actions) == 1
        and isinstance(actions[0], dict)
        and actions[0].get("command") in expected_commands
        and "accept" in request.get("availableDecisions", [])
    )


async def test_real_connector_codex_owner_file_tools_and_original_work(
    bridge, tmp_path, monkeypatch
):
    # This separate package is installed only for explicit live acceptance.
    # The ordinary Bot dependency set never imports or installs the connector.
    from yuki_agent_connector.config import Config, Instance
    from yuki_agent_connector.connector import Connector
    from yuki_agent_connector.journal import Journal

    binary = os.environ.get("YUKI_CODEX_BINARY")
    assert binary, "YUKI_CODEX_BINARY must explicitly select the existing native Codex executable"
    native_program = await asyncio.create_subprocess_exec(
        binary,
        "--version",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    version, version_error = await native_program.communicate()
    assert native_program.returncode == 0, version_error.decode(errors="replace")
    cwd = tmp_path / "isolated-native-project"
    cwd.mkdir()
    marker = f"YUKI_LOCAL_AGENT_BRIDGE_{uuid4().hex}"
    target = cwd / "probe.txt"
    quoted_target = "'" + target.as_posix().replace("'", "''") + "'"
    write_command = (
        f"[System.IO.File]::WriteAllText({quoted_target}, '{marker}', "
        "[System.Text.UTF8Encoding]::new($false))"
    )
    read_command = (
        f"[System.IO.File]::ReadAllText({quoted_target}, [System.Text.UTF8Encoding]::new($false))"
    )
    expected_commands = {write_command, read_command}
    approve_isolated = os.environ.get("YUKI_REAL_CODEX_APPROVE_ISOLATED") == "1"
    approved = set()
    monkeypatch.setenv("SSL_CERT_FILE", str(bridge.ca_file))
    monkeypatch.setenv("NO_PROXY", "localhost,127.0.0.1,::1")
    monkeypatch.setenv("no_proxy", "localhost,127.0.0.1,::1")
    spec = Instance(
        id="explicit-live-codex",
        driver="codex",
        label="Isolated native acceptance",
        command=binary,
        cwd=str(cwd),
        socket=os.environ.get("YUKI_CODEX_SOCKET"),
        endpoint=os.environ.get("YUKI_CODEX_ENDPOINT"),
    )
    config = Config(
        bridge.websocket_url, bridge.device["device_id"], bridge.device["token"], [spec]
    )
    journal = Journal(
        tmp_path / "connector-state" / "journal.sqlite3",
        namespace=json.dumps([config.url, config.device_id]),
    )
    connector = Connector(config, journal)
    running = asyncio.create_task(connector.run())
    operation_id = str(uuid4())
    native_session = None
    finished = False
    driver = None
    try:
        async with asyncio.timeout(30):
            # The real connector has no readiness RPC; observe its actual attachment.
            while not bridge.service.connections.online(config.device_id):
                if running.done():
                    running.result()
                await asyncio.sleep(0.01)
        assert spec.id in connector.drivers, connector.errors
        driver = connector.drivers[spec.id]
        catalog = await bridge.service.execute({"action": "list"}, str(uuid4()), bridge.source)
        instance = next(row["id"] for row in catalog["instances"] if row["label"] == spec.label)
        async with accepted_work(bridge) as control:
            async with asyncio.timeout(180):
                accepted = await bridge.service.execute(
                    {
                        "action": "prompt",
                        "instance_id": instance,
                        "prompt": (
                            f"This is an isolated integration test. Work only in {cwd}. "
                            "Use the PowerShell terminal to run exactly these two commands, "
                            "as two separate tool calls with this working directory. "
                            "Copy each command verbatim; do not combine, alter or prepend "
                            "another command, and do not use apply_patch.\n"
                            f"First command:\n{write_command}\n"
                            f"Second command:\n{read_command}\n"
                            "Do not edit or inspect files outside this directory, "
                            "access the network, "
                            "or change any running service. "
                            f"After verifying the file, finish with exactly {marker}."
                        ),
                    },
                    operation_id,
                    bridge.source,
                )
                assert accepted["operation_id"] == operation_id and accepted["run_id"]
                while True:
                    if running.done():
                        running.result()
                    async with bridge.database.sessions() as reader:
                        operation = await reader.get(AgentOperationModel, operation_id)
                        if operation.session_id:
                            native_session = (
                                await reader.get(
                                    AgentSessionModel,
                                    operation.session_id,
                                )
                            ).native_id
                        task = await reader.get(SandboxTaskRunModel, operation_id)
                        events = list(
                            await reader.scalars(
                                select(AgentEventModel)
                                .where(
                                    AgentEventModel.operation_id == operation_id,
                                )
                                .order_by(AgentEventModel.id)
                            )
                        )
                    for event in events:
                        if event.kind not in {"permission", "question"} or event.id in approved:
                            continue
                        question = json.loads(event.payload_json).get("payload", {})
                        assert (
                            approve_isolated
                            and event.kind == "permission"
                            and question.get("native_method")
                            == "item/commandExecution/requestApproval"
                            and isolated_command_approval(
                                question.get("native_request", {}), cwd, expected_commands
                            )
                        ), (
                            "Native question is outside this test's explicit exact-command grant; "
                            "no approval or global policy change was submitted"
                        )
                        response = await bridge.service.execute(
                            {
                                "action": "respond",
                                "operation_id": operation_id,
                                "permission_id": str(event.id),
                                "response": {"decision": "accept"},
                            },
                            f"test-approval:{operation_id}:{event.id}",
                            bridge.source,
                        )
                        assert response.get("response_submitted") or response.get("admitted")
                        approved.add(event.id)
                    if task.status == "completed":
                        break
                    await asyncio.sleep(0.02)
            completion = json.loads(task.completion_json)
            assert completion["status"] == "succeeded", completion.get("error")
            assert target.read_text(encoding="utf-8") == marker
            assert marker in completion.get("text", "")
            finished = True
            assert operation.native_execution_id
            assert completion["run_id"] == accepted["run_id"]
            payloads = [json.loads(event.payload_json) for event in events]
            assert any(
                event["kind"] == "progress"
                and event.get("payload", {}).get("item", {}).get("type")
                in {"commandExecution", "fileChange"}
                for event in payloads
            ), "A real tool/file milestone must traverse the connector, TLS and server writer"
            commands_seen = [
                event.get("payload", {}).get("item", {}).get("command")
                for event in payloads
                if event.get("payload", {}).get("item", {}).get("type") == "commandExecution"
            ]
            assert all(
                any(
                    expected in (actual if isinstance(actual, str) else " ".join(actual or []))
                    for actual in commands_seen
                )
                for expected in expected_commands
            ), "Both exact write and read commands must have real native tool receipts"
            assert all(
                event.get("execution_id") in (None, operation.native_execution_id)
                for event in payloads
            )
            assert not any('"reasoning"' in json.dumps(event) for event in payloads)
            await SandboxContinuationWorker(control.repository).drain_once()
            async with bridge.database.sessions() as reader:
                original_input = (
                    (
                        await reader.execute(
                            select(inputs).where(
                                inputs.c.work_id == control.current["id"],
                                inputs.c.kind == "completion",
                            )
                        )
                    )
                    .mappings()
                    .one()
                )
            forwarded = json.loads(json.loads(original_input["payload_json"])["text"])
            assert forwarded["run_id"] == accepted["run_id"]
            report = {
                "codex_version": version.decode().strip(),
                "connector_version": "0.1.0",
                "transport": "TLS WebSocket",
                "status": completion["status"],
                "operation_id": operation_id,
                "work_id": control.current["id"],
                "run_id": accepted["run_id"],
                "session_id": operation.session_id,
                "native_execution_confirmed": True,
                "event_count": len(events),
                "tool_milestones": sum(
                    payload.get("payload", {}).get("item", {}).get("type")
                    in {"commandExecution", "fileChange"}
                    for payload in payloads
                ),
                "file_sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
                "completion_forwarded_to_original_work": True,
                "cache_usage": "unknown",
                "isolated_approvals": sorted(approved),
            }
            destination = Path(
                os.environ.get("YUKI_REAL_CODEX_REPORT", tmp_path / "live-result.json")
            )
            await asyncio.to_thread(
                destination.write_text, json.dumps(report, indent=2), encoding="utf-8"
            )
            print(f"Real local Codex bridge evidence: {destination}")
    finally:
        # This test never stops the shared daemon or touches a pre-existing native thread.
        failure = sys.exception()
        try:
            async with asyncio.timeout(20):
                if driver is not None:
                    async with bridge.database.sessions() as reader:
                        original = await reader.get(AgentOperationModel, operation_id)
                        if original is not None and original.session_id:
                            native_session = (
                                await reader.get(
                                    AgentSessionModel,
                                    original.session_id,
                                )
                            ).native_id
                    if (
                        native_session
                        and not finished
                        and original
                        and original.native_execution_id
                    ):
                        await bridge.service.execute(
                            {"action": "cancel", "operation_id": operation_id},
                            f"test-cleanup:{operation_id}",
                            bridge.source,
                        )
                    if native_session:
                        await driver.rpc.call("thread/archive", {"threadId": native_session})
        except Exception as cleanup_error:
            if failure is None:
                raise
            failure.add_note(f"Original native cleanup: {type(cleanup_error).__name__}")
        finally:
            connector.stopped.set()
            if connector.stream is not None:
                await connector.stream.close()
            if not running.done():
                running.cancel()
            await asyncio.gather(running, return_exceptions=True)
            journal.close()
