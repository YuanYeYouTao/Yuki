"""Native retirement keeps its original owner; ordinary Shell cancellation remains live."""

import asyncio
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import update
from tests.integration.test_external_agent_bridge import (
    NativeComputer,
    accepted_work,
    directory,
)
from tests.integration.test_external_agent_bridge import bridge as bridge
from tests.integration.test_local_device_transport import actual_connector, python_command

from yuki.client_api.db_models import ClientDeviceModel
from yuki.runtime.subagent_repository import SubagentRepository
from yuki.runtime.work_schema_v1 import work
from yuki.sandbox.client import SandboxClient
from yuki.services.subagent_execution import SubagentExecution


def executor(control, client):
    return SubagentExecution(
        control.repository,
        SubagentRepository(control.repository),
        SimpleNamespace(sandbox_client=client),
    )


async def stop_work(bridge, identity):
    async with bridge.database.immediate_session() as writer:
        await writer.execute(update(work).where(work.c.id == identity).values(state="cancelled"))


@pytest.mark.parametrize("revoked", [False, True])
async def test_native_retirement_is_not_duplicated_by_shell_cancel_scan(bridge, tmp_path, revoked):
    computer = await NativeComputer(bridge).start()
    try:
        instance, session = await directory(bridge)
        async with accepted_work(bridge) as control:
            operation_id = str(uuid4())
            await bridge.service.execute(
                {
                    "action": "prompt",
                    "instance_id": instance,
                    "session_id": session,
                    "prompt": "Native protocol fixture, no model",
                },
                operation_id,
                bridge.source,
            )
        await stop_work(bridge, control.current["id"])
        if revoked:
            async with bridge.database.immediate_session() as writer:
                device = await writer.get(ClientDeviceModel, bridge.device["device_id"])
                device.revoked_at = datetime.now(UTC)
        original = await bridge.service.tasks.get(operation_id)
        before = (original.run_id, original.source_json, original.progress_json)
        client = SandboxClient(tmp_path / "not-the-native-owner", tasks=bridge.service.tasks)
        client.local_devices = bridge.service.local_devices

        await executor(control, client).cancel_commands()

        assert [command["action"] for command in computer.commands] == ["list", "prompt"]
        await bridge.service.reconcile()
        await asyncio.gather(*bridge.service._retiring.values())
        actions = [command["action"] for command in computer.commands]
        if revoked:
            assert actions == ["list", "prompt"]
            retained = await bridge.service.tasks.get(operation_id)
            assert retained.status == "waiting" and retained.completion_json is None
            assert (retained.run_id, retained.source_json, retained.progress_json) == before
        else:
            assert actions == ["list", "prompt", "cancel"]
            command = computer.commands[-1]
            assert command["arguments"]["operation_id"] == operation_id
            assert command["id"] == f"external-retire:{operation_id}:{operation_id}:cancel"
            await computer.event(operation_id, "completed", {"status": "cancelled"})
            retained = await bridge.service.tasks.get(operation_id)
            assert retained.status == "completed" and retained.run_id == original.run_id
            assert json.loads(retained.completion_json)["status"] == "cancelled"
    finally:
        await computer.close()


async def test_local_shell_without_native_operation_still_cancels_original_process(
    bridge, tmp_path, monkeypatch
):
    async with actual_connector(bridge, tmp_path, monkeypatch):
        client = SandboxClient(tmp_path / "not-the-local-owner", tasks=bridge.service.tasks)
        client.local_devices = bridge.service.local_devices
        script = tmp_path / "running.py"
        script.write_text("import time\nprint('running',flush=True)\ntime.sleep(60)\n")
        async with accepted_work(bridge) as control:
            source = {
                **bridge.source,
                "work_id": control.current["id"],
                "conversation_id": control.lease.conversation_id,
                "generation": control.lease.generation,
            }
            result = await client.execute(
                "terminal_exec",
                {"device_id": bridge.device["device_id"], "command": python_command(script)},
                request_id="original-local-shell",
                source=source,
            )
        assert result["pending"]
        await stop_work(bridge, control.current["id"])

        await executor(control, client).cancel_commands()

        async with asyncio.timeout(8):
            while True:
                retained = await bridge.service.tasks.get("original-local-shell")
                if retained.status == "completed":
                    break
                await asyncio.sleep(0.01)
        assert retained.run_id == result["run_id"]
        assert json.loads(retained.completion_json)["status"] == "cancelled"
