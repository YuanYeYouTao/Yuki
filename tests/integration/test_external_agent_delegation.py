"""Phase-2 failure cases for the one subtask tool set (F3/F5 on records, F7, F8, F10,
F14, F15, F18, F19, F20, observe records and release), and the full-access default of
the 2026-11-20 task book (B1-B5: a delegation that omits scope runs with full access).

Real TLS device WebSocket, real SQLite Work rows, the real Work control tools; the
native peer is the bridge suite's explicit protocol double (no model is called).
"""

import asyncio
import json
from uuid import uuid4

import pytest
from sqlalchemy import select, update
from tests.integration.test_external_agent_bridge import (
    NativeComputer,
    accepted_work,
    directory,
    settled,
)
from tests.integration.test_external_agent_bridge import bridge as bridge

from yuki.external_agents.db_models import AgentOperationModel
from yuki.runtime.subagent_repository import SubagentRepository
from yuki.runtime.subagent_schema import children
from yuki.runtime.work_repository import WorkRepository
from yuki.runtime.work_schema_v1 import inputs, work
from yuki.runtime.work_wait import WorkWaitRepository

FULL = {"write_paths": ["/"], "network": True, "commands": ["*"]}


async def _start(control, key="call-start", **extra):
    outcome = await control.tool_outcome("start_subtask", {"goal": "Fix the fixture", **extra}, key)
    return outcome


async def _row(bridge, work_id):
    async with bridge.database.read_session() as reader:
        return (await reader.execute(select(work).where(work.c.id == work_id))).mappings().one()


async def _inputs(bridge, work_id):
    async with bridge.database.read_session() as reader:
        return list(
            (
                await reader.execute(
                    select(inputs).where(inputs.c.work_id == work_id).order_by(inputs.c.id)
                )
            ).mappings()
        )


async def _operations(bridge, work_id):
    async with bridge.database.read_session() as reader:
        return list(
            await reader.scalars(
                select(AgentOperationModel)
                .where(AgentOperationModel.work_id == work_id)
                .order_by(AgentOperationModel.created_at)
            )
        )


async def _delegate(bridge, control, instance, session_id=None, key="call-start", **extra):
    arguments = {"executor": {"agent": instance}, **extra}
    if session_id is not None:
        arguments["session"] = session_id
    outcome = await _start(control, key, **arguments)
    assert outcome.ok, outcome
    await settled(bridge)
    identity = outcome.data["id"]
    [operation] = [item for item in await _operations(bridge, identity) if item.action == "prompt"]
    return identity, operation


async def _set_state(bridge, work_id, state):
    async with bridge.database.immediate_session() as writer:
        await writer.execute(update(work).where(work.c.id == work_id).values(state=state))


async def test_delegation_is_a_record_with_no_yuki_loop_and_progress_stays_on_file(bridge):
    computer = await NativeComputer(bridge).start()
    try:
        instance, session_id = await directory(bridge)
        async with accepted_work(bridge) as control:
            identity, operation = await _delegate(bridge, control, instance, session_id)
            record = await _row(bridge, identity)
            assert record["parent_work_id"] == control.current["id"]
            assert record["state"] == "waiting_external"
            async with bridge.database.read_session() as reader:
                executor = json.loads(
                    await reader.scalar(
                        select(children.c.executor_json).where(children.c.work_id == identity)
                    )
                )
            assert executor["relationship"] == "delegate" and executor["instance_id"] == instance
            [prompt] = [item for item in computer.commands if item["action"] == "prompt"]
            assert prompt["arguments"]["prompt"] == "Fix the fixture"
            await _set_state(bridge, control.current["id"], "waiting_external")
            for text in ("reading", "editing", "testing"):
                await computer.event(operation.id, "progress", {"text": text})
            # F3 / F4 on a record: nobody wakes; no native JSON reaches any Work input.
            assert await _inputs(bridge, control.current["id"]) == []
            assert await _inputs(bridge, identity) == []
            assert (await _row(bridge, control.current["id"]))["state"] == "waiting_external"
            assert (await _row(bridge, identity))["state"] == "waiting_external"
    finally:
        await computer.close()


async def test_question_wakes_the_parent_with_a_card_and_completion_settles_the_record(bridge):
    computer = await NativeComputer(bridge).start()
    try:
        instance, session_id = await directory(bridge)
        async with accepted_work(bridge) as control:
            identity, operation = await _delegate(bridge, control, instance, session_id)
            outcome = await control.tool_outcome("wait", {"for": [{"run": identity}]}, "wait-run")
            assert outcome.ok, outcome
            await _set_state(bridge, control.current["id"], "waiting_external")
            await computer.event(
                operation.id,
                "permission",
                {
                    "native_request_id": "write-outside",
                    "tool": "Write",
                    "options": ["allow", "deny"],
                },
            )
            assert (await _row(bridge, identity))["state"] == "waiting_user"
            parent = await _row(bridge, control.current["id"])
            assert parent["state"] == "queued"
            [card_input] = await _inputs(bridge, control.current["id"])
            card = json.loads(json.loads(card_input["payload_json"])["text"])
            assert card["kind"] == "external_agent_card" and card["pending"]["tool"] == "Write"
            assert card["permission_id"] and card["read"] == {"get_tasks": {"id": identity}}
            # F10: Yuki's answer records who answered, the option, the reason and the request.
            await _set_state(bridge, control.current["id"], "running")
            answered = await control.tool_outcome(
                "message_task",
                {
                    "id": identity,
                    "reply_to": card["permission_id"],
                    "answer": "allow",
                    "text": "Writing the report next to the fixture is expected",
                },
                "answer-1",
            )
            assert answered.ok, answered
            await settled(bridge)
            [respond] = [item for item in computer.commands if item["action"] == "respond"]
            assert respond["arguments"]["permission_id"] == "write-outside"
            view = await control.tool_outcome("get_tasks", {"id": identity}, "read-1")
            entries = [
                entry
                for entry in view.data["timeline"]["operations"]
                if entry["action"] == "respond"
            ]
            assert entries == [
                {
                    **entries[0],
                    "answered_by": "yuki",
                    "option": "allow",
                    "reason": "Writing the report next to the fixture is expected",
                    "request": {"kind": "permission", "tool": "Write"},
                }
            ]
            assert (await _row(bridge, identity))["state"] == "waiting_external"
            assert view.data["card"]["pending"] == []
            # The approval never changed the fixed scope of the record.
            async with bridge.database.read_session() as reader:
                executor = json.loads(
                    await reader.scalar(
                        select(children.c.executor_json).where(children.c.work_id == identity)
                    )
                )
            assert executor["scope"] == FULL
            await computer.event(operation.id, "completed", {"status": "completed", "text": "Done"})
            record = await _row(bridge, identity)
            assert record["state"] == "completed"
            assert json.loads(json.loads(record["checkpoint_json"])["sync_result"])["summary"] == (
                "Done"
            )
            await SubagentRepository(WorkRepository(bridge.database)).maintain()
            results = [
                json.loads(item["payload_json"])
                for item in await _inputs(bridge, control.current["id"])
                if item["source_key"].startswith("worker-result:")
            ]
            assert len(results) == 1
    finally:
        await computer.close()


async def test_f5_root_waiting_for_messages_is_woken_by_its_delegations_question(bridge):
    computer = await NativeComputer(bridge).start()
    try:
        instance, session_id = await directory(bridge)
        async with accepted_work(bridge) as control:
            identity, operation = await _delegate(bridge, control, instance, session_id)
            outcome = await control.tool_outcome("wait", {"for": [{"message": True}]}, "wait-msg")
            assert outcome.ok, outcome
            await _set_state(bridge, control.current["id"], "waiting_user")
            await computer.event(operation.id, "progress", {"text": "still going"})
            assert (await _row(bridge, control.current["id"]))["state"] == "waiting_user"
            await computer.event(
                operation.id, "question", {"native_request_id": "q", "question": "?"}
            )
            assert (await _row(bridge, control.current["id"]))["state"] == "queued"
            assert identity
    finally:
        await computer.close()


async def test_f6_wait_registered_after_completion_is_satisfied_at_once(bridge):
    computer = await NativeComputer(bridge).start()
    try:
        instance, session_id = await directory(bridge)
        async with accepted_work(bridge) as control:
            identity, operation = await _delegate(bridge, control, instance, session_id)
            await computer.event(operation.id, "completed", {"status": "completed", "text": "ok"})
            outcome = await control.tool_outcome("wait", {"for": [{"run": identity}]}, "wait-late")
            assert outcome.ok, outcome
            delivered = await WorkWaitRepository(control.repository).deliver_due()
            assert delivered == 1
    finally:
        await computer.close()


async def test_f7_second_delegation_never_takes_an_owned_session(bridge):
    computer = await NativeComputer(bridge).start()
    try:
        instance, session_id = await directory(bridge)
        async with accepted_work(bridge) as control:
            identity, _operation = await _delegate(bridge, control, instance, session_id)
            second = await _start(
                control, "call-second", executor={"agent": instance}, session=session_id
            )
            assert not second.ok and second.error_code == "conflict"
            assert identity in second.public_message
            await settled(bridge)
            assert [item["action"] for item in computer.commands].count("prompt") == 1
    finally:
        await computer.close()


async def test_f8_scoped_delegation_never_runs_on_a_connector_without_scope(bridge):
    unscoped = NativeComputer(bridge)
    unscoped.capabilities = [item for item in unscoped.capabilities if item != "scope"]
    computer = await unscoped.start()
    try:
        instance, session_id = await directory(bridge)
        async with accepted_work(bridge) as control:
            outcome = await _start(
                control,
                executor={"agent": instance},
                session=session_id,
                scope={"write_paths": ["/tmp/fixture"], "network": False, "commands": ["pytest"]},
            )
            assert not outcome.ok and outcome.error_code == "connector_outdated"
            # B4: the full-access default is a scope too; never run without it.
            omitted = await _start(
                control, "call-omitted", executor={"agent": instance}, session=session_id
            )
            assert not omitted.ok and omitted.error_code == "connector_outdated"
            await settled(bridge)
            assert not any(item["action"] == "prompt" for item in computer.commands)
            async with bridge.database.read_session() as reader:
                states = list(
                    await reader.scalars(
                        select(work.c.state)
                        .join(children, children.c.work_id == work.c.id)
                        .where(children.c.executor_json.is_not(None))
                    )
                )
            assert states == ["failed", "failed"]
        await computer.close()
        computer = await NativeComputer(bridge).start()
        async with accepted_work(bridge) as control:
            outcome = await _start(
                control,
                "call-scoped",
                executor={"agent": instance},
                session=session_id,
                scope={"write_paths": ["/tmp/fixture"], "network": False, "commands": ["pytest"]},
            )
            assert outcome.ok, outcome
            await settled(bridge)
            [prompt] = [item for item in computer.commands if item["action"] == "prompt"]
            assert prompt["arguments"]["scope"] == {
                "write_paths": ["/tmp/fixture"],
                "network": False,
                "commands": ["pytest"],
            }
    finally:
        await computer.close()


async def test_f14_f15_lost_executor_is_told_once_stays_unknown_and_real_terminal_wins(bridge):
    computer = await NativeComputer(bridge).start()
    try:
        instance, session_id = await directory(bridge)
        async with accepted_work(bridge) as control:
            identity, operation = await _delegate(bridge, control, instance, session_id)
            await _set_state(bridge, control.current["id"], "waiting_external")
            lost = {"status": "unknown", "reason": "native_sdk_connection_lost"}
            await computer.event(operation.id, "progress", lost)
            await computer.event(operation.id, "progress", lost)
            record = await _row(bridge, identity)
            assert record["state"] == "waiting_external"
            assert record["reason"] == "native_sdk_connection_lost"
            told = await _inputs(bridge, control.current["id"])
            assert len(told) == 1
            view = await control.tool_outcome("get_tasks", {"id": identity}, "read-lost")
            assert view.data["card"]["state"] == "unknown"
            assert [item["action"] for item in computer.commands].count("prompt") == 1
            await computer.event(operation.id, "completed", {"status": "completed", "text": "ok"})
            assert (await _row(bridge, identity))["state"] == "completed"
    finally:
        await computer.close()


async def test_f19_desktop_answer_settles_directly_once_and_yuki_cannot_answer_again(bridge):
    from yuki.client_api.works import ClientWorks

    computer = await NativeComputer(bridge).start()
    try:
        instance, session_id = await directory(bridge)
        async with accepted_work(bridge) as control:
            identity, operation = await _delegate(bridge, control, instance, session_id)
            await computer.event(
                operation.id, "permission", {"native_request_id": "native-1", "tool": "Bash"}
            )
            card = json.loads(
                json.loads((await _inputs(bridge, control.current["id"]))[0]["payload_json"])[
                    "text"
                ]
            )
            principal = await bridge.chat.authenticate(bridge.chat_device["token"])
            works = ClientWorks(bridge.chat, {})
            receipt = await works.answer(
                principal, identity, card["permission_id"], str(uuid4()), "allow", None
            )
            assert receipt["permission_id"] == card["permission_id"]
            await settled(bridge)
            responds = [item for item in computer.commands if item["action"] == "respond"]
            assert len(responds) == 1 and responds[0]["arguments"]["option_id"] == "allow"
            again = await control.tool_outcome(
                "message_task",
                {"id": identity, "reply_to": card["permission_id"], "answer": "deny"},
                "late-answer",
            )
            assert not again.ok and again.error_code == "request_already_answered"
            await settled(bridge)
            assert [item["action"] for item in computer.commands].count("respond") == 1
            view = await control.tool_outcome("get_tasks", {"id": identity}, "read-answer")
            [entry] = [
                item for item in view.data["timeline"]["operations"] if item["action"] == "respond"
            ]
            assert entry["answered_by"] == "user" and entry["answered_via"] == "desktop"
    finally:
        await computer.close()


async def test_f20_events_of_two_delegations_on_one_device_never_cross(bridge):
    computer = await NativeComputer(bridge).start()
    try:
        instance, _session_id = await directory(bridge)
        async with accepted_work(bridge) as control:
            first, first_op = await _delegate(bridge, control, instance, key="call-a", cwd="/a")
            second, second_op = await _delegate(bridge, control, instance, key="call-b", cwd="/b")
            await computer.event(first_op.id, "progress", {"text": "A"})
            await computer.event(second_op.id, "progress", {"text": "B"})
            first_view = await control.tool_outcome("get_tasks", {"id": first}, "read-a")
            second_view = await control.tool_outcome("get_tasks", {"id": second}, "read-b")
            assert [
                event["payload"]["text"] for event in first_view.data["timeline"]["events"]
            ] == ["A"]
            assert [
                event["payload"]["text"] for event in second_view.data["timeline"]["events"]
            ] == ["B"]
    finally:
        await computer.close()


async def test_observation_never_inputs_answers_or_controls_and_tells_the_user(bridge):
    computer = await NativeComputer(bridge).start()
    try:
        instance, session_id = await directory(bridge)
        async with accepted_work(bridge) as control:
            control.current = None  # observe needs no current Work
            outcome = await _start(
                control,
                "call-observe",
                executor={"agent": instance},
                session=session_id,
                relationship="observe",
                goal="Watch the user's terminal session",
            )
            assert outcome.ok, outcome
            await settled(bridge)
            identity = outcome.data["id"]
            record = await _row(bridge, identity)
            assert record["parent_work_id"] is None and record["state"] == "waiting_external"
            [watch] = [item for item in computer.commands if item["action"] == "watch"]
            assert watch["arguments"]["observation"] == "observe"
            # B3: an observation never carries a scope.
            assert "scope" not in watch["arguments"]
            [operation] = await _operations(bridge, identity)
            await computer.event(
                operation.id, "permission", {"native_request_id": "terminal-1", "tool": "Bash"}
            )
            await computer.event(
                operation.id,
                "permission",
                {"native_request_id": "terminal-1", "tool": "Bash"},
            )
            async with bridge.database.read_session() as reader:
                notices = list(
                    (
                        await reader.execute(
                            select(work).where(
                                work.c.source_key.like(f"observe-notice:{identity}%")
                            )
                        )
                    ).mappings()
                )
            assert len(notices) == 1 and notices[0]["state"] == "queued"
            assert notices[0]["parent_work_id"] is None
            [notice_input] = await _inputs(bridge, notices[0]["id"])
            card = json.loads(json.loads(notice_input["payload_json"])["text"])
            assert "permission_id" not in card
            assert card["pending"]["answer_at"] == "native_terminal"
            refused = await control.tool_outcome(
                "message_task", {"id": identity, "text": "inject"}, "inject"
            )
            assert not refused.ok and refused.error_code == "observe_only"
            stopped = await control.tool_outcome(
                "manage_task", {"action": "cancel", "id": identity}, "stop-observe"
            )
            assert stopped.ok, stopped
            await settled(bridge)
            actions = [item["action"] for item in computer.commands]
            assert "respond" not in actions and "prompt" not in actions and "queue" not in actions
            assert "cancel" not in actions and actions[-1] == "detach"
            assert (await _row(bridge, identity))["state"] == "cancelled"
    finally:
        await computer.close()


async def test_release_ends_ownership_closes_the_owner_and_never_cancels_native_work(bridge):
    computer = await NativeComputer(bridge).start()
    try:
        instance, session_id = await directory(bridge)
        async with accepted_work(bridge) as control:
            identity, operation = await _delegate(bridge, control, instance, session_id)
            released = await control.tool_outcome(
                "manage_task", {"action": "release", "id": identity}, "release-1"
            )
            assert released.ok, released
            await settled(bridge)
            [detach] = [item for item in computer.commands if item["action"] == "detach"]
            assert detach["arguments"] == {"operation_id": operation.id, "release": True}
            assert not any(item["action"] == "cancel" for item in computer.commands)
            record = await _row(bridge, identity)
            assert record["state"] == "cancelled" and record["reason"] == "released"
            async with bridge.database.read_session() as reader:
                stored = await reader.get(AgentOperationModel, operation.id)
            assert stored.detached_at is not None
            await bridge.service.reconcile()
            await settled(bridge)
            assert not any(item["action"] == "cancel" for item in computer.commands)
    finally:
        await computer.close()


def test_f18_retired_coding_agent_tools_are_not_declared():
    from yuki.runtime.subagent_tools import subagent_tools
    from yuki.runtime.work_control import work_control_tools

    names = {tool.name for tool in (*subagent_tools(), *work_control_tools())}
    assert {"use_coding_agent", "get_coding_agents"}.isdisjoint(names)
    with pytest.raises(ModuleNotFoundError):
        __import__("yuki.external_agents.tools")


async def test_f18_a_registered_legacy_run_still_settles_after_the_cutover(bridge):
    computer = await NativeComputer(bridge).start()
    try:
        instance, session_id = await directory(bridge)
        async with accepted_work(bridge) as control:
            operation_id = str(uuid4())
            legacy = await bridge.service.execute(
                {
                    "action": "prompt",
                    "instance_id": instance,
                    "session_id": session_id,
                    "prompt": "Registered before the cutover",
                },
                operation_id,
                bridge.source,
            )
            await settled(bridge)
            outcome = await control.tool_outcome(
                "wait", {"for": [{"run": legacy["run_id"]}]}, "wait-legacy"
            )
            assert outcome.ok, outcome
            await computer.event(operation_id, "completed", {"status": "completed", "text": "ok"})
            assert await WorkWaitRepository(control.repository).deliver_due() == 1
    finally:
        await computer.close()


async def _followup_event(computer, owner_id, turn, kind, payload, *, pending=True):
    computer.sequence += 1
    event = {
        "type": "event",
        "event_id": str(uuid4()),
        "instance_id": computer.local_id,
        "operation_id": None,
        "owner_operation_id": owner_id,
        "followup_execution": turn,
        "session_id": computer.native_session,
        "execution_id": turn,
        "kind": kind,
        "payload": payload,
        "sequence": computer.sequence,
        "operation_state": {
            "status": "running" if pending else "completed",
            "pending": pending,
            "session_id": computer.native_session,
            "execution_id": turn,
            "state_sequence": computer.sequence,
        },
    }
    await computer.socket.send(json.dumps(event))
    async with asyncio.timeout(5):
        assert await computer.acknowledged.get() == event["event_id"]
    return event


# F16: a self-started turn of the delegation's own native owner is attributed to
# another delegation, another Work, or to nobody (its permission then waits unseen).
async def test_f16_self_started_turn_belongs_to_the_delegation_that_owns_the_process(bridge):
    computer = await NativeComputer(bridge).start()
    try:
        instance, session_id = await directory(bridge)
        async with accepted_work(bridge) as control:
            identity, operation = await _delegate(bridge, control, instance, session_id)
            other, other_op = await _delegate(bridge, control, instance, key="call-other", cwd="/o")
            await computer.event(operation.id, "completed", {"status": "completed", "text": "ok"})
            assert (await _row(bridge, identity))["state"] == "completed"
            await _set_state(bridge, control.current["id"], "waiting_external")
            await _followup_event(
                computer,
                operation.id,
                "wakeup-turn",
                "permission",
                {"native_request_id": "bash-after-wakeup", "tool": "Bash", "connection_id": "c1"},
            )
            followup = f"followup:{operation.id}:wakeup-turn"
            async with bridge.database.read_session() as reader:
                stored = await reader.get(AgentOperationModel, followup)
            assert stored is not None and stored.work_id == identity
            assert (await _row(bridge, identity))["state"] == "waiting_user"
            assert (await _row(bridge, other))["state"] == "waiting_external"
            cards = [
                json.loads(json.loads(item["payload_json"])["text"])
                for item in await _inputs(bridge, control.current["id"])
                if item["source_key"].startswith(f"delegation:{identity}:")
            ]
            assert len(cards) == 1 and cards[0]["permission_id"]
            await _set_state(bridge, control.current["id"], "running")
            answered = await control.tool_outcome(
                "message_task",
                {"id": identity, "reply_to": cards[0]["permission_id"], "answer": "deny"},
                "answer-followup",
            )
            assert answered.ok, answered
            await settled(bridge)
            [respond] = [item for item in computer.commands if item["action"] == "respond"]
            assert respond["arguments"]["operation_id"] == followup
            assert respond["arguments"]["owner_operation_id"] == operation.id
            assert other_op.id not in json.dumps(respond)
    finally:
        await computer.close()


# Recovery: a subtask control committed natively whose Work result write (T3) was lost
# reads its original receipt by call key; it is neither replayed nor left unknown.
async def test_lost_result_of_a_delegation_control_reads_its_original_receipt(bridge):
    from types import SimpleNamespace

    from yuki.capabilities.invocation import direct_invocations
    from yuki.domain.messages import ToolCall, ToolFunction
    from yuki.runtime.effect_outcomes import ResultCapture, current_result_capture
    from yuki.runtime.work_journal import WorkJournal

    computer = await NativeComputer(bridge).start()
    try:
        instance, session_id = await directory(bridge)
        async with accepted_work(bridge) as control:
            identity, _operation = await _delegate(bridge, control, instance, session_id)
            args = {"id": identity, "text": "also run lint", "mode": "steer"}
            invocation = direct_invocations(
                (ToolCall("steer-call", ToolFunction("message_task", json.dumps(args))),),
                SimpleNamespace(work_control=control),
                manifest_revision="contract",
            )[0]
            key = invocation.identity.operation_id
            await control.repository.prepare_effect(
                control.lease,
                control.current["id"],
                key,
                "tool",
                invocation=invocation.durable_metadata(),
            )
            assert await control.repository.admit_dispatch(
                control.lease, control.current["id"], key
            )
            assert (await control.tool_outcome("message_task", args, key)).ok
            await settled(bridge)
            capture = ResultCapture(control.current["id"], key)
            token = current_result_capture.set(capture)
            try:
                value = json.loads(await WorkJournal(control.repository).effect_result(key))
            finally:
                current_result_capture.reset(token)
            assert value["action"] == "steer" and value["replay_forbidden"] is True
            assert value["admitted"] is True and value.get("error") is None
            assert [item["action"] for item in computer.commands].count("steer") == 1
    finally:
        await computer.close()


# Review B1: releasing an idle delegation (its turn already finished) still reaches the
# device, for every operation it holds, so the native process can be closed (G9, F13).
async def test_release_of_an_idle_delegation_reaches_the_device(bridge):
    computer = await NativeComputer(bridge).start()
    try:
        instance, session_id = await directory(bridge)
        async with accepted_work(bridge) as control:
            identity, operation = await _delegate(bridge, control, instance, session_id)
            await computer.event(operation.id, "completed", {"status": "completed", "text": "ok"})
            released = await control.tool_outcome(
                "manage_task", {"action": "release", "id": identity}, "release-idle"
            )
            assert released.ok, released
            await settled(bridge)
            [detach] = [item for item in computer.commands if item["action"] == "detach"]
            assert detach["arguments"] == {"operation_id": operation.id, "release": True}
            assert detach["session_id"] == computer.native_session
    finally:
        await computer.close()


# Review B2: every dispatch of a scoped delegation carries its fixed scope (§4.4),
# including text added later and a resume on the same session.
async def test_follow_up_input_and_resume_carry_the_delegation_scope(bridge):
    await _follow_ups_carry(
        bridge, {"write_paths": ["/tmp/fixture"], "network": False, "commands": ["pytest"]}
    )


# B1/B2/B5: a delegation that omits scope gets full access (not the model's choice),
# stores it and sends it with every later input; an explicit scope is kept as given.
async def test_delegation_without_scope_runs_with_full_access(bridge):
    await _follow_ups_carry(bridge, None)


async def _follow_ups_carry(bridge, given):
    scope = FULL if given is None else given
    computer = await NativeComputer(bridge).start()
    try:
        instance, session_id = await directory(bridge)
        async with accepted_work(bridge) as control:
            extra = {} if given is None else {"scope": given}
            identity, operation = await _delegate(bridge, control, instance, session_id, **extra)
            async with bridge.database.read_session() as reader:
                executor = json.loads(
                    await reader.scalar(
                        select(children.c.executor_json).where(children.c.work_id == identity)
                    )
                )
            assert executor["scope"] == scope
            added = await control.tool_outcome(
                "message_task", {"id": identity, "text": "also lint"}, "scoped-queue"
            )
            assert added.ok, added
            await settled(bridge)
            await computer.event(operation.id, "completed", {"status": "completed", "text": "ok"})
            for item in [i for i in computer.commands if i["action"] == "queue"]:
                await computer.event(item["id"], "completed", {"status": "completed"})
            resumed = await control.tool_outcome(
                "manage_task",
                {"action": "resume", "id": identity, "text": "one more"},
                "scoped-resume",
            )
            assert resumed.ok, resumed
            await settled(bridge)
            inputs_sent = [i for i in computer.commands if i["action"] in {"prompt", "queue"}]
            assert len(inputs_sent) == 3
            assert all(item["arguments"].get("scope") == scope for item in inputs_sent)
    finally:
        await computer.close()


# A live Claude session is refused by the device; an explicit fork starts a new session
# and the record binds to the new session, never conflicting with the original (S7).
async def test_explicit_fork_binds_the_record_to_the_new_session(bridge):
    computer = await NativeComputer(bridge).start()
    try:
        instance, session_id = await directory(bridge)
        async with accepted_work(bridge) as control:
            computer.hold = {"prompt"}
            outcome = await _start(
                control, "call-fork", executor={"agent": instance}, session=session_id, fork=True
            )
            assert outcome.ok, outcome
            await settled(bridge)
            [prompt] = [item for item in computer.commands if item["action"] == "prompt"]
            assert prompt["arguments"]["fork"] is True and prompt["session_id"]
            identity = outcome.data["id"]
            [operation] = await _operations(bridge, identity)
            forked = {
                "execution_id": computer.native_execution,
                "status": "running",
                "pending": True,
                "admitted": True,
                "session_id": "forked-native-session",
                "forked_from": computer.native_session,
            }
            await computer.answer(prompt["id"], forked)
            await settled(bridge)
            [operation] = await _operations(bridge, identity)
            async with bridge.database.read_session() as reader:
                from yuki.external_agents.db_models import AgentSessionModel

                bound = await reader.get(AgentSessionModel, operation.session_id)
            assert bound.native_id == "forked-native-session"
    finally:
        await computer.close()
