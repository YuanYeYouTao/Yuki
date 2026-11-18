"""Failure cases from the independent server review (taskbook §16), written first.

Each test names the review finding it guards against.
"""

import json
from uuid import uuid4

from sqlalchemy import select
from tests.integration.test_external_agent_bridge import (
    NativeComputer,
    accepted_work,
    directory,
    settled,
    until,
)
from tests.integration.test_external_agent_bridge import bridge as bridge
from tests.integration.test_external_agent_delegation import (
    _delegate,
    _inputs,
    _row,
    _set_state,
    _start,
)

from yuki.external_agents.db_models import AgentOperationModel
from yuki.external_agents.delegation import Delegations
from yuki.runtime.subagent_repository import SubagentRepository
from yuki.runtime.work_repository import WorkRepository
from yuki.runtime.work_schema_v1 import work
from yuki.sandbox.continuation_worker import SandboxContinuationWorker


async def _card(bridge, identity):
    async with bridge.database.read_session() as reader:
        return await Delegations(bridge.database).card(reader, identity)


def _texts(rows):
    return [json.loads(row["payload_json"])["text"] for row in rows]


# 1: a refused first dispatch leaves a zombie record (queued, "running", parent never told).
async def test_refused_start_fails_the_record_and_tells_the_parent(bridge):
    computer = await NativeComputer(bridge).start()
    try:
        instance, session_id = await directory(bridge)
        async with accepted_work(bridge) as control:
            computer.reject_next = "prompt"
            outcome = await _start(
                control, "refused", executor={"agent": instance}, session=session_id
            )
            assert outcome.ok, outcome
            identity = outcome.data["id"]
            await settled(bridge)
            await SandboxContinuationWorker(WorkRepository(bridge.database)).drain_once()
            record = await _row(bridge, identity)
            assert record["state"] == "failed"
            parent = [row["source_key"] for row in await _inputs(bridge, control.current["id"])]
            assert any(key.startswith(f"worker-result:{identity}:") for key in parent)
            assert not any(key.startswith("completion:") for key in parent)
            assert await _inputs(bridge, identity) == []
    finally:
        await computer.close()


# 1b: one finished run of a record with another still running never queues the record.
async def test_a_finished_run_never_queues_a_record_with_another_running(bridge):
    computer = await NativeComputer(bridge).start()
    try:
        instance, session_id = await directory(bridge)
        async with accepted_work(bridge) as control:
            identity, operation = await _delegate(bridge, control, instance, session_id)
            added = await control.tool_outcome(
                "message_task", {"id": identity, "text": "more"}, "more-input"
            )
            assert added.ok, added
            await settled(bridge)
            await computer.event(operation.id, "completed", {"status": "completed", "text": "a"})
            await SandboxContinuationWorker(WorkRepository(bridge.database)).drain_once()
            assert (await _row(bridge, identity))["state"] == "waiting_external"
    finally:
        await computer.close()


# 2: Codex integer request ids: an answered request stays pending and can be answered again.
async def test_integer_native_request_ids_are_answered_once(bridge):
    computer = await NativeComputer(bridge).start()
    try:
        instance, session_id = await directory(bridge)
        async with accepted_work(bridge) as control:
            identity, operation = await _delegate(bridge, control, instance, session_id)
            await computer.event(operation.id, "permission", {"native_request_id": 5, "tool": "x"})
            card = json.loads(_texts(await _inputs(bridge, control.current["id"]))[0])
            await _set_state(bridge, control.current["id"], "running")
            first = await control.tool_outcome(
                "message_task",
                {"id": identity, "reply_to": card["permission_id"], "answer": "accept"},
                "int-1",
            )
            assert first.ok, first
            await settled(bridge)
            assert (await _card(bridge, identity))["pending"] == []
            again = await control.tool_outcome(
                "message_task",
                {"id": identity, "reply_to": card["permission_id"], "answer": "decline"},
                "int-2",
            )
            assert not again.ok and again.error_code == "request_already_answered"
    finally:
        await computer.close()


# 3: a re-delivered request wakes twice; a Codex serverRequest/resolved never resumes the record.
async def test_redelivered_request_wakes_once_and_native_resolution_resumes(bridge):
    computer = await NativeComputer(bridge).start()
    try:
        instance, session_id = await directory(bridge)
        async with accepted_work(bridge) as control:
            identity, operation = await _delegate(bridge, control, instance, session_id)
            for _ in range(2):
                await computer.event(
                    operation.id, "permission", {"native_request_id": 9, "tool": "x"}
                )
            assert len(await _inputs(bridge, control.current["id"])) == 1
            assert (await _row(bridge, identity))["state"] == "waiting_user"
            await computer.event(
                operation.id,
                "progress",
                {"native_method": "serverRequest/resolved", "native": {"requestId": 9}},
            )
            assert (await _row(bridge, identity))["state"] == "waiting_external"
    finally:
        await computer.close()


# 4: after a lost executor the card still says running.
async def test_lost_executor_card_is_unknown(bridge):
    computer = await NativeComputer(bridge).start()
    try:
        instance, session_id = await directory(bridge)
        async with accepted_work(bridge) as control:
            identity, operation = await _delegate(bridge, control, instance, session_id)
            await computer.event(
                operation.id,
                "progress",
                {"status": "unknown", "reason": "native_acp_process_exited"},
            )
            assert (await _card(bridge, identity))["state"] == "unknown"
    finally:
        await computer.close()


# 5: message_task / resume report success although nothing was dispatched.
async def test_undispatched_message_and_resume_are_reported_as_failures(bridge):
    computer = await NativeComputer(bridge).start()
    try:
        instance, session_id = await directory(bridge)
        async with accepted_work(bridge) as control:
            identity, operation = await _delegate(bridge, control, instance, session_id)
            await computer.event(operation.id, "completed", {"status": "completed", "text": "ok"})
            await computer.close()
            device = bridge.device["device_id"]

            async def offline():
                return not bridge.service.connections.online(device)

            await until(offline)
            resumed = await control.tool_outcome(
                "manage_task", {"action": "resume", "id": identity, "text": "again"}, "resume"
            )
            assert not resumed.ok and resumed.error_code == "device_offline"
            assert (await _row(bridge, identity))["state"] == "completed"
    finally:
        await computer.close()


# 7: a connector restart's restored snapshot (status unknown) wakes the parent.
async def test_restored_snapshot_is_not_an_executor_loss(bridge):
    computer = await NativeComputer(bridge).start()
    try:
        instance, session_id = await directory(bridge)
        async with accepted_work(bridge) as control:
            identity, operation = await _delegate(bridge, control, instance, session_id)
            await computer.event(
                operation.id,
                "progress",
                {"status": "unknown", "reason": "native_snapshot_restored"},
            )
            assert await _inputs(bridge, control.current["id"]) == []
            assert identity
    finally:
        await computer.close()


# 8: several observed facts at once start several notice Mains instead of one.
async def test_observation_notices_coalesce_while_one_is_queued(bridge):
    computer = await NativeComputer(bridge).start()
    try:
        instance, session_id = await directory(bridge)
        async with accepted_work(bridge) as control:
            control.current = None
            outcome = await _start(
                control,
                "observe",
                executor={"agent": instance},
                session=session_id,
                relationship="observe",
            )
            assert outcome.ok, outcome
            await settled(bridge)
            identity = outcome.data["id"]
            async with bridge.database.read_session() as reader:
                watch = await reader.scalar(
                    select(AgentOperationModel.id).where(AgentOperationModel.work_id == identity)
                )
            await computer.event(watch, "permission", {"native_request_id": "a", "tool": "Bash"})
            await computer.event(watch, "question", {"native_request_id": "b", "tool": "Ask"})
            async with bridge.database.read_session() as reader:
                notices = list(
                    await reader.scalars(
                        select(work.c.id).where(
                            work.c.source_key.like(f"observe-notice:{identity}%")
                        )
                    )
                )
            assert len(notices) == 1
            assert len(await _inputs(bridge, notices[0])) == 2
    finally:
        await computer.close()


# 10: release's detach is dropped because the record ended before the write.
async def test_release_detach_is_written_even_after_the_record_ends(bridge, monkeypatch):
    from yuki.external_agents.service import ExternalAgentService

    original = ExternalAgentService._dispatch

    async def late(service, identity, connection, command):
        # The record's cancellation commits before this write is checked.
        await __import__("asyncio").sleep(0.2)
        return await original(service, identity, connection, command)

    computer = await NativeComputer(bridge).start()
    try:
        instance, session_id = await directory(bridge)
        async with accepted_work(bridge) as control:
            identity, _operation = await _delegate(bridge, control, instance, session_id)
            monkeypatch.setattr(ExternalAgentService, "_dispatch", late)
            released = await control.tool_outcome(
                "manage_task", {"action": "release", "id": identity}, "release-late"
            )
            assert released.ok, released
            await settled(bridge)
            assert [item["action"] for item in computer.commands].count("detach") == 1
    finally:
        await computer.close()


# 13: the Desktop answer reports an undispatched answer as an operation.
async def test_desktop_answer_while_offline_is_an_error(bridge):
    import pytest

    from yuki.client_api.errors import ClientError
    from yuki.client_api.works import ClientWorks

    computer = await NativeComputer(bridge).start()
    try:
        instance, session_id = await directory(bridge)
        async with accepted_work(bridge) as control:
            identity, operation = await _delegate(bridge, control, instance, session_id)
            await computer.event(
                operation.id, "permission", {"native_request_id": "n", "tool": "x"}
            )
            card = json.loads(_texts(await _inputs(bridge, control.current["id"]))[0])
            await computer.close()
            device = bridge.device["device_id"]

            async def offline():
                return not bridge.service.connections.online(device)

            await until(offline)
            principal = await bridge.chat.authenticate(bridge.chat_device["token"])
            with pytest.raises(ClientError, match="device_offline"):
                await ClientWorks(bridge.chat, {}).answer(
                    principal, identity, card["permission_id"], str(uuid4()), "allow", None
                )
    finally:
        await computer.close()


# F2: the Desktop can answer an observed request.
async def test_desktop_answer_of_an_observed_request_is_refused(bridge):
    import pytest

    from yuki.client_api.errors import ClientError
    from yuki.client_api.works import ClientWorks

    computer = await NativeComputer(bridge).start()
    try:
        instance, session_id = await directory(bridge)
        async with accepted_work(bridge) as control:
            control.current = None
            outcome = await _start(
                control,
                "obs",
                executor={"agent": instance},
                session=session_id,
                relationship="observe",
            )
            await settled(bridge)
            identity = outcome.data["id"]
            async with bridge.database.read_session() as reader:
                watch = await reader.scalar(
                    select(AgentOperationModel.id).where(AgentOperationModel.work_id == identity)
                )
            event = await computer.event(watch, "permission", {"native_request_id": "o"})
            from yuki.external_agents.db_models import AgentEventModel

            async with bridge.database.read_session() as reader:
                permission = await reader.scalar(
                    select(AgentEventModel.id).where(AgentEventModel.event_id == event["event_id"])
                )
            principal = await bridge.chat.authenticate(bridge.chat_device["token"])
            with pytest.raises(ClientError, match="observe_only"):
                await ClientWorks(bridge.chat, {}).answer(
                    principal, identity, str(permission), str(uuid4()), "allow", None
                )
            assert not any(item["action"] == "respond" for item in computer.commands)
    finally:
        await computer.close()


# F6: a request that arrived before the parent registers wait(run) still wakes it at once.
async def test_request_before_wait_satisfies_the_new_wait(bridge):
    computer = await NativeComputer(bridge).start()
    try:
        instance, session_id = await directory(bridge)
        async with accepted_work(bridge) as control:
            identity, operation = await _delegate(bridge, control, instance, session_id)
            await computer.event(
                operation.id, "question", {"native_request_id": "q", "question": "?"}
            )
            outcome = await control.tool_outcome("wait", {"for": [{"run": identity}]}, "late-wait")
            assert outcome.ok, outcome
            assert await control.has_pending_business_inputs()
    finally:
        await computer.close()


# F15: offline → online → the real terminal fact settles the record (no re-dispatch).
async def test_offline_then_reconnect_then_real_terminal_settles(bridge):
    computer = await NativeComputer(bridge).start()
    try:
        instance, session_id = await directory(bridge)
        async with accepted_work(bridge) as control:
            identity, operation = await _delegate(bridge, control, instance, session_id)
            await computer.close()
            device = bridge.device["device_id"]

            async def offline():
                return not bridge.service.connections.online(device)

            await until(offline)

            async def told():
                # The offline fact is written right after the socket is detached.
                return (await _row(bridge, identity))["reason"] == "device_offline"

            await until(told)
            assert (await _card(bridge, identity))["state"] == "unknown"
            computer = await NativeComputer(bridge).start()

            async def back():
                return (await _card(bridge, identity))["state"] == "running"

            await until(back)
            await computer.event(operation.id, "completed", {"status": "completed", "text": "ok"})
            assert (await _row(bridge, identity))["state"] == "completed"
            assert [item["action"] for item in computer.commands].count("prompt") == 0
    finally:
        await computer.close()


# 15: a parent's own release/cancel is settled without waking it with a result.
async def test_parent_initiated_end_does_not_wake_the_parent(bridge):
    computer = await NativeComputer(bridge).start()
    try:
        instance, session_id = await directory(bridge)
        async with accepted_work(bridge) as control:
            identity, _operation = await _delegate(bridge, control, instance, session_id)
            released = await control.tool_outcome(
                "manage_task", {"action": "release", "id": identity}, "release-own"
            )
            assert released.ok, released
            await _set_state(bridge, control.current["id"], "waiting_external")
            await SubagentRepository(WorkRepository(bridge.database)).maintain()
            assert (await _row(bridge, control.current["id"]))["state"] == "waiting_external"
            assert await _inputs(bridge, control.current["id"]) == []
    finally:
        await computer.close()


async def _unowned(computer, native_request_id):
    import asyncio

    computer.sequence += 1
    event = {
        "type": "event",
        "event_id": str(uuid4()),
        "instance_id": computer.local_id,
        "operation_id": None,
        "session_id": computer.native_session,
        "execution_id": "self-started",
        "kind": "permission",
        "payload": {
            "permission_id": native_request_id,
            "native_request_id": native_request_id,
            "connection_id": "owner-1",
            "tool": "Bash",
            "options": ["deny"],
            "unowned": True,
        },
        "sequence": computer.sequence,
    }
    await computer.socket.send(json.dumps(event))
    async with asyncio.timeout(5):
        assert await computer.acknowledged.get() == event["event_id"]


# 6: an unowned wait after release reaches nobody, and nothing can deny it any more.
async def test_unowned_wait_reaches_the_last_owner_and_is_deny_only(bridge):
    computer = await NativeComputer(bridge).start()
    try:
        instance, session_id = await directory(bridge)
        async with accepted_work(bridge) as control:
            identity, _operation = await _delegate(bridge, control, instance, session_id)
            released = await control.tool_outcome(
                "manage_task", {"action": "release", "id": identity}, "release-then-unowned"
            )
            assert released.ok, released
            await settled(bridge)
            await _unowned(computer, "late-bash")
            cards = [
                json.loads(text)
                for text in _texts(await _inputs(bridge, control.current["id"]))
                if "late-bash" in text or "deny_only" in text
            ]
            assert len(cards) == 1 and cards[0]["pending"]["answer_at"] == "deny_only"
            refused = await control.tool_outcome(
                "message_task",
                {"id": identity, "reply_to": cards[0]["permission_id"], "answer": "allow"},
                "unowned-allow",
            )
            assert not refused.ok and refused.error_code == "deny_only"
            denied = await control.tool_outcome(
                "message_task",
                {"id": identity, "reply_to": cards[0]["permission_id"], "answer": "deny"},
                "unowned-deny",
            )
            assert denied.ok, denied
            await settled(bridge)
            [respond] = [item for item in computer.commands if item["action"] == "respond"]
            assert respond["arguments"]["option_id"] == "deny"
            assert respond["arguments"]["permission_id"] == "late-bash"
    finally:
        await computer.close()


# 12: a receipt the device cannot own disconnects the device forever (it is retransmitted).
async def test_mismatched_receipt_is_dropped_without_closing_the_device(bridge):
    import asyncio

    computer = await NativeComputer(bridge).start()
    try:
        await directory(bridge)
        forged = {
            "type": "event",
            "event_id": str(uuid4()),
            "instance_id": computer.local_id,
            "kind": "receipt",
            "operation_id": "not-an-operation-of-this-device",
            "payload": {"result": {"status": "completed", "pending": False}},
        }
        computer.receipts[forged["event_id"]] = forged
        await computer.socket.send(json.dumps(forged))
        async with asyncio.timeout(5):
            assert await computer.receipt_acks.get() == forged["event_id"]
        assert bridge.service.connections.online(bridge.device["device_id"])
    finally:
        await computer.close()


# 9: a cleanup control refused before writing is never written on a later pass.
async def test_cleanup_control_refused_before_writing_is_written_on_a_new_connection(bridge):
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
                    "prompt": "cleanup",
                },
                target,
                bridge.source,
            )
            await settled(bridge)
            original = bridge.service.repository.dispatchable

            async def refuse(identity):
                return False

            bridge.service.repository.dispatchable = refuse
            await bridge.service.cleanup_tasks([target], "cleanup-pass")
            await settled(bridge)
            bridge.service.repository.dispatchable = original
            assert not any(item["action"] == "cancel" for item in computer.commands)
            await computer.close()
            computer = await NativeComputer(bridge).start()
            await bridge.service.cleanup_tasks([target], "cleanup-pass")
            await settled(bridge)
            assert [item["action"] for item in computer.commands].count("cancel") == 1
    finally:
        await computer.close()


# 14: a record is managed by the person it acts for, from any of their conversations,
# and never by another person.
async def test_another_person_cannot_manage_a_record(bridge):
    from tests.support.canonical_identity import ensure_person

    computer = await NativeComputer(bridge).start()
    try:
        instance, session_id = await directory(bridge)
        async with accepted_work(bridge) as control:
            identity, _operation = await _delegate(bridge, control, instance, session_id)
            async with bridge.database.immediate_session() as writer:
                stranger = await ensure_person(writer, "30003")
            owner = control.source["actor_person_id"]
            control.source["actor_person_id"] = stranger
            refused = await control.tool_outcome(
                "message_task", {"id": identity, "text": "inject"}, "stranger-message"
            )
            control.source["actor_person_id"] = owner
            assert not refused.ok and refused.error_code == "subtask_not_owned"
            assert not any(item["action"] == "queue" for item in computer.commands)
    finally:
        await computer.close()
