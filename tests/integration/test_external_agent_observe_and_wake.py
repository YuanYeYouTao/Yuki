"""Phase-2 failure case F1 (written before the implementation).

Real TLS device WebSocket and SQLite Work inputs/waits, as in the bridge suite.
Each test names the failure it guards against; none relies on timing thresholds.
"""

from uuid import uuid4

import pytest
from sqlalchemy import select
from tests.integration.test_external_agent_bridge import (
    NativeComputer,
    accepted_work,
    directory,
    settled,
)
from tests.integration.test_external_agent_bridge import bridge as bridge

from yuki.external_agents.errors import ExternalAgentArgumentError
from yuki.runtime.work_schema_v1 import inputs, work
from yuki.runtime.work_wait_schema import waits


async def _work_inputs(bridge, work_id):
    async with bridge.database.read_session() as reader:
        return list(
            (
                await reader.execute(
                    select(inputs).where(inputs.c.work_id == work_id).order_by(inputs.c.id)
                )
            ).mappings()
        )


async def _work_state(bridge, work_id):
    async with bridge.database.read_session() as reader:
        return await reader.scalar(select(work.c.state).where(work.c.id == work_id))


async def _active_waits(bridge, work_id):
    async with bridge.database.read_session() as reader:
        return list(
            (
                await reader.execute(
                    select(waits).where(waits.c.work_id == work_id, waits.c.status == "active")
                )
            ).mappings()
        )


async def _watch(bridge, instance, session_id):
    operation_id = str(uuid4())
    await bridge.service.execute(
        {"action": "watch", "instance_id": instance, "session_id": session_id},
        operation_id,
        bridge.source,
    )
    await settled(bridge)
    return operation_id


async def _prompt(bridge, instance, session_id):
    operation_id = str(uuid4())
    result = await bridge.service.execute(
        {
            "action": "prompt",
            "instance_id": instance,
            "session_id": session_id,
            "prompt": "Isolated fixture task",
        },
        operation_id,
        bridge.source,
    )
    await settled(bridge)
    return operation_id, result["run_id"]


# F1: an observe relationship answers (allow or deny), prompts, queues, steers or cancels.
@pytest.mark.parametrize("option", ["allow", "deny"])
async def test_f1_observed_permission_is_never_answered(bridge, option):
    computer = await NativeComputer(bridge).start()
    try:
        instance, session_id = await directory(bridge)
        async with accepted_work(bridge) as control:
            watch_id = await _watch(bridge, instance, session_id)
            await computer.event(
                watch_id,
                "permission",
                {
                    "native_request_id": "terminal-owned-request",
                    "tool": "Bash",
                    "options": [{"id": "allow"}, {"id": "deny"}],
                },
            )
            from yuki.external_agents.db_models import AgentEventModel

            async with bridge.database.read_session() as reader:
                event_row = await reader.scalar(
                    select(AgentEventModel).where(AgentEventModel.operation_id == watch_id)
                )
            sent = len(computer.commands)
            with pytest.raises(ExternalAgentArgumentError, match="observe_only"):
                await bridge.service.execute(
                    {
                        "action": "respond",
                        "operation_id": watch_id,
                        "permission_id": str(event_row.id),
                        "option_id": option,
                    },
                    str(uuid4()),
                    bridge.source,
                )
            await settled(bridge)
            assert len(computer.commands) == sent
            assert control.current is not None
    finally:
        await computer.close()


@pytest.mark.parametrize("action", ["prompt", "queue", "steer", "cancel"])
async def test_f1_observed_operation_never_targets_input_or_native_control(bridge, action):
    computer = await NativeComputer(bridge).start()
    try:
        instance, session_id = await directory(bridge)
        async with accepted_work(bridge):
            watch_id = await _watch(bridge, instance, session_id)
            sent = len(computer.commands)
            with pytest.raises(ExternalAgentArgumentError, match="observe_only"):
                await bridge.service.execute(
                    {"action": action, "operation_id": watch_id, "prompt": "Injected"},
                    str(uuid4()),
                    bridge.source,
                )
            await settled(bridge)
            assert len(computer.commands) == sent
    finally:
        await computer.close()
