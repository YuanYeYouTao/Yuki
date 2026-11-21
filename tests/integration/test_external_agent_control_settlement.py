"""#668 failure cases a control's settlement must keep (written before the fix).

R2: a resent receipt adds no second input. R3: the Work waiting on the control's
still-waiting run is interrupted at once; a control that ends the run leaves it to
the completion. R4: on a delegation record a failed control is told to the parent
once, a successful one tells nobody. (R1, the receipt and the input committed together,
is `test_external_agent_bridge.py::test_detach_receipt_commits_with_the_works_control_input`.)

Real TLS device WebSocket and SQLite Work inputs/waits, as in the bridge suite.
"""

import asyncio
import json
from uuid import uuid4

from sqlalchemy import select, update
from tests.integration.test_external_agent_bridge import (
    NativeComputer,
    accepted_work,
    directory,
    receipt,
    settled,
    until,
)
from tests.integration.test_external_agent_bridge import bridge as bridge
from tests.integration.test_external_agent_delegation import _delegate, _operations

from yuki.runtime.work_schema_v1 import inputs, work
from yuki.runtime.work_wait import WorkWaitRepository
from yuki.runtime.work_wait_schema import waits
from yuki.sandbox.continuation_worker import SandboxContinuationWorker
from yuki.sandbox.db_models import SandboxTaskRunModel


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


async def _waiting_on(bridge, control, run_id):
    outcome = await control.tool_outcome(
        "wait", {"for": [{"run": run_id}, {"seconds": 3600}]}, f"wait-{uuid4()}"
    )
    assert outcome.ok, outcome
    async with bridge.database.immediate_session() as writer:
        await writer.execute(
            update(work).where(work.c.id == control.current["id"]).values(state="waiting_external")
        )


async def _inputs(bridge, work_id, prefix):
    async with bridge.database.read_session() as reader:
        return list(
            (
                await reader.execute(
                    select(inputs.c.source_key, inputs.c.kind, inputs.c.payload_json).where(
                        inputs.c.work_id == work_id, inputs.c.source_key.like(f"{prefix}%")
                    )
                )
            ).mappings()
        )


async def _waits(bridge, work_id):
    async with bridge.database.read_session() as reader:
        return list(await reader.scalars(select(waits.c.status).where(waits.c.work_id == work_id)))


async def _resend_receipt(computer, operation_id):
    """The device sends its receipt for this control again (its ACK was lost)."""
    [event] = [item for item in computer.receipts.values() if item["operation_id"] == operation_id]
    await computer.socket.send(json.dumps(event))
    async with asyncio.timeout(5):
        while await computer.receipt_acks.get() != event["event_id"]:
            pass


async def test_r2_r3_steer_on_a_waited_run_tells_and_interrupts_once(bridge):
    computer = await NativeComputer(bridge).start()
    try:
        instance, session_id = await directory(bridge)
        async with accepted_work(bridge) as control:
            operation_id, run_id = await _prompt(bridge, instance, session_id)
            await _waiting_on(bridge, control, run_id)
            assert await _waits(bridge, control.current["id"]) == ["active"]
            steer = str(uuid4())
            await bridge.service.execute(
                {"action": "steer", "operation_id": operation_id, "prompt": "Adjust"},
                steer,
                bridge.source,
            )
            await settled(bridge)
            [told] = await _inputs(bridge, control.current["id"], f"external-agent:control:{steer}")
            card = json.loads(json.loads(told["payload_json"])["text"])
            assert card["action"] == "steer" and card["target_operation_id"] == operation_id
            # The Work waiting on that very run hears about it at once.
            assert await _waits(bridge, control.current["id"]) == ["delivered"]
            async with bridge.database.read_session() as reader:
                state = await reader.scalar(
                    select(work.c.state).where(work.c.id == control.current["id"])
                )
            assert state != "waiting_external"
            stored = await receipt(bridge, steer)
            await _resend_receipt(computer, steer)
            await settled(bridge)
            assert await receipt(bridge, steer) == stored
            assert await _inputs(
                bridge, control.current["id"], f"external-agent:control:{steer}"
            ) == [told]
    finally:
        await computer.close()


async def test_r3_cancel_that_ends_the_run_leaves_the_wait_to_the_completion(bridge):
    computer = await NativeComputer(bridge).start()
    try:
        instance, session_id = await directory(bridge)
        async with accepted_work(bridge) as control:
            operation_id, run_id = await _prompt(bridge, instance, session_id)
            await _waiting_on(bridge, control, run_id)
            computer.hold = {"cancel"}
            cancel = str(uuid4())
            await bridge.service.execute(
                {"action": "cancel", "operation_id": operation_id}, cancel, bridge.source
            )

            async def held():
                return cancel in computer.held

            await until(held)
            await computer.answer(
                cancel, {"status": "cancelled", "pending": False, "admitted": True}
            )
            await settled(bridge)
            [told] = await _inputs(
                bridge, control.current["id"], f"external-agent:control:{cancel}"
            )
            assert json.loads(json.loads(told["payload_json"])["text"])["status"] == "cancelled"
            async with bridge.database.read_session() as reader:
                run = await reader.get(SandboxTaskRunModel, operation_id)
            assert run.status == "completed"
            assert json.loads(run.completion_json)["status"] == "cancelled"
            # The run's own terminal fact settles the wait, not the control.
            assert await _waits(bridge, control.current["id"]) == ["active"]
            await SandboxContinuationWorker(control.repository).drain_once()
            assert len(await _inputs(bridge, control.current["id"], "completion:")) == 1
            await WorkWaitRepository(control.repository).deliver_due()
            assert await _waits(bridge, control.current["id"]) == ["delivered"]
    finally:
        await computer.close()


async def test_r4_failed_control_on_a_delegation_tells_the_parent_once(bridge):
    computer = await NativeComputer(bridge).start()
    try:
        instance, session_id = await directory(bridge)
        async with accepted_work(bridge) as control:
            identity, _operation = await _delegate(bridge, control, instance, session_id)
            computer.fail = {"steer": {"code": "native_busy", "message": "Busy", "executed": False}}
            outcome = await control.tool_outcome(
                "message_task", {"id": identity, "text": "also lint", "mode": "steer"}, "steer-1"
            )
            assert outcome.ok, outcome
            await settled(bridge)
            [steer] = [
                item for item in await _operations(bridge, identity) if item.action == "steer"
            ]
            [told] = await _inputs(bridge, control.current["id"], f"control:{steer.id}")
            assert told["kind"] == "subagent"
            assert json.loads(json.loads(told["payload_json"])["text"])["error"]
            # The record itself is a record, not a model context: no control input.
            assert await _inputs(bridge, identity, "external-agent:control:") == []
            await _resend_receipt(computer, steer.id)
            await settled(bridge)
            assert await _inputs(bridge, control.current["id"], f"control:{steer.id}") == [told]
    finally:
        await computer.close()


async def test_r4_successful_control_on_a_delegation_tells_nobody(bridge):
    computer = await NativeComputer(bridge).start()
    try:
        instance, session_id = await directory(bridge)
        async with accepted_work(bridge) as control:
            identity, _operation = await _delegate(bridge, control, instance, session_id)
            outcome = await control.tool_outcome(
                "message_task", {"id": identity, "text": "also lint", "mode": "steer"}, "steer-1"
            )
            assert outcome.ok, outcome
            await settled(bridge)
            [steer] = [
                item for item in await _operations(bridge, identity) if item.action == "steer"
            ]
            assert (await receipt(bridge, steer.id))["admitted"] is True
            assert await _inputs(bridge, control.current["id"], "control:") == []
            assert await _inputs(bridge, identity, "external-agent:control:") == []
    finally:
        await computer.close()
