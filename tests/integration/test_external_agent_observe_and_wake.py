"""Phase-2 failure cases F1–F6, F17 (written before the implementation).

Real TLS device WebSocket and SQLite Work inputs/waits, as in the bridge suite.
Each test names the failure it guards against; none relies on timing thresholds.
"""

import json
from uuid import uuid4

import pytest
from sqlalchemy import select, update
from tests.integration.test_external_agent_bridge import (
    NativeComputer,
    accepted_work,
    directory,
    settled,
    until,
)
from tests.integration.test_external_agent_bridge import bridge as bridge

from yuki.external_agents.cards import external_agent_card
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


# F2: an observed request reaches the Work with an answerable permission_id.
async def test_f2_observed_request_input_carries_no_permission_id(bridge):
    computer = await NativeComputer(bridge).start()
    try:
        instance, session_id = await directory(bridge)
        async with accepted_work(bridge) as control:
            watch_id = await _watch(bridge, instance, session_id)
            await computer.event(
                watch_id,
                "permission",
                {"native_request_id": "terminal-owned-request", "tool": "Bash"},
            )
            rows = await _work_inputs(bridge, control.current["id"])
            assert len(rows) == 1
            card = json.loads(json.loads(rows[0]["payload_json"])["text"])
            assert "permission_id" not in card
            assert card["relationship"] == "observe"
            assert card["pending"]["answer_at"] == "native_terminal"
    finally:
        await computer.close()


# F3: one progress event wakes the Work or interrupts time / all waits (plugin waits share
# the same signal path; granting a plugin is outside this fixture).
async def test_f3_progress_writes_no_input_and_interrupts_no_wait(bridge):
    computer = await NativeComputer(bridge).start()
    try:
        instance, session_id = await directory(bridge)
        async with accepted_work(bridge) as control:
            operation_id, run_id = await _prompt(bridge, instance, session_id)
            outcome = await control.tool_outcome(
                "wait",
                {
                    "for": [
                        {"run": run_id},
                        {"seconds": 3600},
                    ],
                    "all": True,
                },
                "wait-all",
            )
            assert outcome.ok, outcome
            async with bridge.database.immediate_session() as writer:
                await writer.execute(
                    update(work)
                    .where(work.c.id == control.current["id"])
                    .values(state="waiting_external")
                )
            before = await _active_waits(bridge, control.current["id"])
            for text in ("Testing started", "Editing a file", "Running pytest"):
                await computer.event(operation_id, "progress", {"text": text})
            assert await _work_inputs(bridge, control.current["id"]) == []
            assert await _active_waits(bridge, control.current["id"]) == before
            assert await _work_state(bridge, control.current["id"]) == "waiting_external"
    finally:
        await computer.close()


# F4: native progress JSON reaches the Work's model input.
async def test_f4_progress_text_never_reaches_work_inputs_and_wake_input_is_a_card(bridge):
    computer = await NativeComputer(bridge).start()
    try:
        instance, session_id = await directory(bridge)
        async with accepted_work(bridge) as control:
            operation_id, _run_id = await _prompt(bridge, instance, session_id)
            await computer.event(operation_id, "progress", {"text": "PROGRESS-MARKER"})
            await computer.event(
                operation_id,
                "question",
                {
                    "native_request_id": "native-q",
                    "question": "Which file?",
                    "options": [{"id": "a", "label": "a.py"}],
                    "native_blob": {"raw": "NATIVE-BLOB-MARKER"},
                },
            )
            rows = await _work_inputs(bridge, control.current["id"])
            texts = [json.loads(row["payload_json"])["text"] for row in rows]
            assert len(texts) == 1
            assert "PROGRESS-MARKER" not in texts[0]
            assert "NATIVE-BLOB-MARKER" not in texts[0]
            card = json.loads(texts[0])
            assert card["kind"] == "external_agent_card"
            assert card["pending"]["question"] == "Which file?"
            assert card["pending"]["options"] == [{"id": "a", "label": "a.py"}]
            assert card["permission_id"]
    finally:
        await computer.close()


# F5: a root Work waiting only for messages misses the question; or progress wakes it.
async def test_f5_question_wakes_a_root_waiting_for_messages_and_progress_does_not(bridge):
    computer = await NativeComputer(bridge).start()
    try:
        instance, session_id = await directory(bridge)
        async with accepted_work(bridge) as control:
            operation_id, _run_id = await _prompt(bridge, instance, session_id)
            outcome = await control.tool_outcome("wait", {"for": [{"message": True}]}, "wait-msg")
            assert outcome.ok, outcome
            async with bridge.database.immediate_session() as writer:
                await writer.execute(
                    update(work)
                    .where(work.c.id == control.current["id"])
                    .values(state="waiting_user")
                )
            await computer.event(operation_id, "progress", {"text": "still working"})
            assert await _work_state(bridge, control.current["id"]) == "waiting_user"
            await computer.event(
                operation_id, "question", {"native_request_id": "q1", "question": "Which?"}
            )
            assert await _work_state(bridge, control.current["id"]) == "queued"
    finally:
        await computer.close()


# F6: old progress inputs are signalled again when a wait registers.
async def test_f6_registering_a_wait_never_resignals_old_progress_inputs(bridge):
    computer = await NativeComputer(bridge).start()
    try:
        instance, session_id = await directory(bridge)
        async with accepted_work(bridge) as control:
            _operation_id, run_id = await _prompt(bridge, instance, session_id)
            # A progress input written by the previous release (no wake marker).
            async with bridge.database.immediate_session() as writer:
                await writer.execute(
                    inputs.insert().values(
                        conversation_id=control.lease.conversation_id,
                        generation=control.lease.generation,
                        work_id=control.current["id"],
                        source_key=f"legacy-progress:{uuid4()}",
                        kind="control",
                        ready=True,
                        payload_json=json.dumps(
                            {
                                "text": json.dumps({"kind": "external_agent_event"}),
                                "signal": True,
                                "external_run_id": run_id,
                            }
                        ),
                        created=0.0,
                    )
                )
            outcome = await control.tool_outcome(
                "wait", {"for": [{"run": run_id}, {"seconds": 3600}]}, "wait-run"
            )
            assert outcome.ok, outcome
            active = await _active_waits(bridge, control.current["id"])
            assert len(active) == 1
    finally:
        await computer.close()


# Transport: reconnecting is progress; going offline is a wake fact told once.
async def test_device_connected_is_progress_and_offline_is_one_wake_input(bridge):
    computer = await NativeComputer(bridge).start()
    try:
        instance, session_id = await directory(bridge)
        async with accepted_work(bridge) as control:
            await _prompt(bridge, instance, session_id)
            await computer.close()
            device = bridge.device["device_id"]

            async def offline():
                return not bridge.service.connections.online(device)

            await until(offline)
            computer = await NativeComputer(bridge).start()
            rows = await _work_inputs(bridge, control.current["id"])
            events = [json.loads(json.loads(row["payload_json"])["text"])["event"] for row in rows]
            assert events == ["device_offline"]
    finally:
        await computer.close()


# F17: the same card input encodes to different bytes, or a replay rewrites it.
def test_f17_card_encoding_is_byte_deterministic():
    arguments = {
        "event": "question",
        "operation_id": "op-1",
        "run_id": "run-1",
        "relationship": "delegate",
        "status": "requires_action",
        "payload": {"question": "Which?", "options": [{"label": "x", "id": "1"}], "tool": "Ask"},
        "permission_id": "7",
        "session_id": "s",
        "execution_id": "e",
    }
    first = external_agent_card(**arguments)
    second = external_agent_card(**dict(reversed(list(arguments.items()))))
    assert first == second
    assert list(json.loads(first)) == sorted(json.loads(first))


async def test_f17_replayed_wake_event_never_rewrites_its_input(bridge):
    computer = await NativeComputer(bridge).start()
    try:
        instance, session_id = await directory(bridge)
        async with accepted_work(bridge) as control:
            operation_id, _run_id = await _prompt(bridge, instance, session_id)
            event = await computer.event(
                operation_id, "question", {"native_request_id": "q", "question": "?"}
            )
            first = await _work_inputs(bridge, control.current["id"])
            await computer.event(
                operation_id, "question", event["payload"], event_id=event["event_id"]
            )
            assert await _work_inputs(bridge, control.current["id"]) == first
    finally:
        await computer.close()
