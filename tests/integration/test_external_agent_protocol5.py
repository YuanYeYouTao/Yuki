"""Device protocol 5 (Q4 hard switch): receipts are events settled before their ACK.

Failure cases: a protocol-4 connector is still accepted; a mutating command is
awaited as an RPC; a receipt is ACKed before its settlement commits, or a device
settles another device's operation; a source cleanup sends an unregistered native
control whose answer nobody records.
"""

import asyncio
import json
from uuid import uuid4

import pytest
from sqlalchemy import select
from tests.integration.test_external_agent_bridge import (
    NativeComputer,
    accepted_work,
    directory,
    receipt,
    settled,
)
from tests.integration.test_external_agent_bridge import bridge as bridge
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from yuki.external_agents.db_models import AgentOperationModel


async def test_protocol_4_connector_is_refused(bridge):
    socket = await connect(
        bridge.websocket_url,
        ssl=bridge.ssl,
        proxy=None,
        additional_headers={"Authorization": f"Bearer {bridge.device['token']}"},
    )
    await socket.send(json.dumps({"type": "hello", "protocol_version": 4, "instances": []}))
    with pytest.raises(ConnectionClosed):
        async with asyncio.timeout(5):
            while True:
                await socket.recv()
    assert not bridge.service.connections.online(bridge.device["device_id"])


async def test_receipt_settles_before_ack_and_a_replay_changes_nothing(bridge):
    computer = await NativeComputer(bridge).start()
    try:
        instance, session_id = await directory(bridge)
        async with accepted_work(bridge):
            computer.hold = {"prompt"}
            operation_id = str(uuid4())
            await bridge.service.execute(
                {
                    "action": "prompt",
                    "instance_id": instance,
                    "session_id": session_id,
                    "prompt": "settle by receipt",
                },
                operation_id,
                bridge.source,
            )
            await settled(bridge)
            assert await receipt(bridge, operation_id) is None
            result = {
                "session_id": computer.native_session,
                "execution_id": computer.native_execution,
                "status": "running",
                "pending": True,
                "admitted": True,
            }
            await computer.answer(operation_id, result)
            event_id = next(iter(computer.receipts))
            async with asyncio.timeout(5):
                assert await computer.receipt_acks.get() == event_id
            # The ACK arrived only after the receipt was committed.
            assert (await receipt(bridge, operation_id))["admitted"] is True
            await computer.socket.send(json.dumps(computer.receipts[event_id]))
            async with asyncio.timeout(5):
                assert await computer.receipt_acks.get() == event_id
            assert (await receipt(bridge, operation_id))["admitted"] is True
            assert [item["action"] for item in computer.commands].count("prompt") == 1
    finally:
        await computer.close()


async def test_a_device_never_settles_an_operation_it_does_not_hold(bridge):
    computer = await NativeComputer(bridge).start()
    try:
        instance, session_id = await directory(bridge)
        async with accepted_work(bridge):
            computer.hold = {"prompt"}
            operation_id = str(uuid4())
            await bridge.service.execute(
                {
                    "action": "prompt",
                    "instance_id": instance,
                    "session_id": session_id,
                    "prompt": "held",
                },
                operation_id,
                bridge.source,
            )
            await settled(bridge)
            forged = {
                "type": "event",
                "event_id": str(uuid4()),
                "instance_id": "another-local-instance",
                "kind": "receipt",
                "operation_id": operation_id,
                "payload": {"result": {"status": "completed", "pending": False}},
            }
            computer.receipts[forged["event_id"]] = forged
            await computer.socket.send(json.dumps(forged))
            # Acknowledged and dropped: a refused ACK would be resent forever.
            async with asyncio.timeout(5):
                assert await computer.receipt_acks.get() == forged["event_id"]
            assert bridge.service.connections.online(bridge.device["device_id"])
            assert await receipt(bridge, operation_id) is None
    finally:
        await computer.close()


async def test_source_cleanup_registers_its_control_and_its_receipt_settles_the_target(bridge):
    computer = await NativeComputer(bridge).start()
    try:
        instance, session_id = await directory(bridge)
        async with accepted_work(bridge):
            target = str(uuid4())
            await bridge.service.execute(
                {
                    "action": "prompt",
                    "instance_id": instance,
                    "session_id": session_id,
                    "prompt": "to be cleaned",
                },
                target,
                bridge.source,
            )
            await settled(bridge)
            computer.results.pop(target, None)
            [view] = await bridge.service.cleanup_tasks([target], "cleanup-source")
            await settled(bridge)
            [cancel] = [item for item in computer.commands if item["action"] == "cancel"]
            async with bridge.database.read_session() as reader:
                control = await reader.get(AgentOperationModel, cancel["id"])
                assert control is not None and control.action == "cancel"
                assert json.loads(control.result_json)["admitted"] is True
                assert (
                    await reader.scalar(
                        select(AgentOperationModel.id).where(AgentOperationModel.id == target)
                    )
                    == target
                )
            assert view["operation_id"] == cancel["id"]
    finally:
        await computer.close()
