"""#645: a released session's unowned waits are settled by the release (deny).

Failure cases written before the implementation (taskbook
``Yuki-外部Agent交还时未答原生请求结算任务书-2026-11-20`` §5, revision 3):
- F1-F4, F7: the release does not deny the wait, wakes the parent, leaves no record
  on the released record, or denies twice on a redelivered fact;
- N1-N4: a forged or misused marker lets a command through ``dispatchable``;
- R1, R2: a session taken over by another delegation is still denied by the release,
  including a takeover between the settlement's check and its registration, after
  which the release's own attempt must not make it the session's holder again;
- R4: an observation started after the release (it watches, never answers) stops
  the settlement, so the turn is stuck again;
- R5: a release that a later resume of the same record ended still denies;
- T1: an answer whose delivery is unknown is never written again on a reconnect, or
  is written under a new ID (a second native answer); a resend that cannot be
  written (connection replaced, grant gone) records "not executed" although the
  device may have run the first write;
- T2: a transient refusal (owner_disconnected / session_not_loaded / not_live) ends
  the settlement, or is retried by every maintenance tick instead of at the next
  fact (another wait in that session, or a reconnect);
- T3: request_resolved or a native resolution does not end it;
- T4: a wait in another session retries this released session's refused answers;
- T5: a (re)connect waits for settlement work.
Nothing about a settlement is kept in memory, so a bot restart is a reconnect (T1).
"""

import asyncio
import json
from uuid import uuid4

from sqlalchemy import select
from tests.integration.test_external_agent_bridge import (
    NativeComputer,
    accepted_work,
    directory,
    settled,
)
from tests.integration.test_external_agent_bridge import bridge as bridge
from tests.integration.test_external_agent_delegation import _delegate, _inputs, _start
from tests.integration.test_external_agent_review_fixes import _card, _texts, _unowned

from yuki.external_agents.db_models import AgentOperationModel
from yuki.external_agents.delegation import Delegations, last_holder, release_answer
from yuki.runtime.work_schema_v1 import work

SUBMITTED = {
    "status": "response_submitted",
    "response_submitted": True,
    "pending": False,
    "admitted": True,
}


async def _released(bridge, control, instance, session_id, key="release"):
    identity, operation = await _delegate(bridge, control, instance, session_id)
    released = await control.tool_outcome("manage_task", {"action": "release", "id": identity}, key)
    assert released.ok, released
    await settled(bridge)
    return identity, operation


def _responds(*computers):
    return [
        item for computer in computers for item in computer.commands if item["action"] == "respond"
    ]


async def _settle(bridge):
    """One maintenance tick after every receipt sent so far has been settled.

    Maintenance never settles a release by itself; tests use it to prove that.
    """
    await settled(bridge)
    await bridge.service.reconcile()
    await settled(bridge)


async def _attempts(bridge, identity):
    async with bridge.database.read_session() as reader:
        return list(
            await reader.scalars(
                select(AgentOperationModel)
                .where(
                    AgentOperationModel.work_id == identity,
                    AgentOperationModel.action == "respond",
                )
                .order_by(AgentOperationModel.created_at, AgentOperationModel.id)
            )
        )


async def _resolved(computer, native_request_id, outcome="withdrawn"):
    computer.sequence += 1
    event = {
        "type": "event",
        "event_id": str(uuid4()),
        "instance_id": computer.local_id,
        "operation_id": None,
        "session_id": computer.native_session,
        "execution_id": "released-turn",
        "kind": "progress",
        "payload": {"permission_resolved": native_request_id, "outcome": outcome, "pending": True},
        "sequence": computer.sequence,
    }
    await computer.socket.send(json.dumps(event))
    async with asyncio.timeout(5):
        assert await computer.acknowledged.get() == event["event_id"]


# F1-F4, F7
async def test_a_released_sessions_wait_is_denied_once_on_the_record_and_wakes_nobody(bridge):
    computer = await NativeComputer(bridge).start()
    computer.hold = {"respond"}
    try:
        instance, session_id = await directory(bridge)
        async with accepted_work(bridge) as control:
            identity, _ = await _released(bridge, control, instance, session_id)
            before = len(await _inputs(bridge, control.current["id"]))
            event = await _unowned(computer, "late-bash")
            await settled(bridge)
            [respond] = _responds(computer)
            assert respond["session_id"] == computer.native_session
            assert respond["arguments"]["option_id"] == "deny"
            assert respond["arguments"]["permission_id"] == "late-bash"
            assert respond["arguments"]["connection_id"] == "owner-1"
            assert "response" not in respond["arguments"]
            assert "operation_id" not in respond["arguments"]
            # Nobody is asked: no card, no wake.
            later = _texts(await _inputs(bridge, control.current["id"]))[before:]
            assert not any("deny_only" in text or "late-bash" in text for text in later)
            await computer.answer(respond["id"], SUBMITTED)
            await settled(bridge)
            async with bridge.database.read_session() as reader:
                timeline = await Delegations(bridge.database).timeline(reader, identity, 0, 50)
            [answer] = [item for item in timeline["operations"] if item["action"] == "respond"]
            assert answer["answered_via"] == "release" and answer["option"] == "deny"
            assert answer["request"] == {"kind": "permission", "tool": "Bash"}
            assert (await _card(bridge, identity))["pending"] == []
            # A redelivered fact (lost ACK) never answers again.
            await _unowned(computer, "late-bash", event)
            await _settle(bridge)
            assert len(_responds(computer)) == 1
            assert later == _texts(await _inputs(bridge, control.current["id"]))[before:]
    finally:
        await computer.close()


async def _forge(
    bridge,
    identity,
    session_id,
    instance,
    *,
    action="respond",
    arguments=None,
    source=None,
    work_id=None,
    operation_id=None,
):
    """Register a row directly, as only an internal caller could, to test dispatchable."""
    async with bridge.database.read_session() as reader:
        release = await reader.scalar(
            select(AgentOperationModel.id).where(
                AgentOperationModel.work_id == identity, AgentOperationModel.action == "detach"
            )
        )
    arguments = (
        {"option_id": "deny", "permission_id": "late-bash"} if arguments is None else arguments
    )
    source = (
        {
            "actor_person_id": bridge.source["actor_person_id"],
            "work_id": work_id or identity,
            "settled_by": "release",
            "release_operation_id": release,
        }
        if source is None
        else source
    )
    async with bridge.database.read_session() as reader:
        conversation = await reader.scalar(
            select(work.c.conversation_id).where(work.c.id == identity)
        )
    row, _ = await bridge.service.repository.prepare(
        identity=operation_id or f"forged:{uuid4()}",
        instance_id=instance,
        actor=bridge.source["actor_person_id"],
        action=action,
        arguments=arguments,
        command={"type": "command", "id": "x", "action": action, "arguments": arguments},
        source={"conversation_id": conversation, "generation": 1, **source},
        session_id=session_id,
        native_execution_id=None,
        lease=None,
    )
    return row.id, release


# N1-N4 (and the positive control they are measured against)
async def test_dispatchable_accepts_only_a_release_deny_proven_by_facts(bridge):
    computer = await NativeComputer(bridge).start()
    try:
        instance, session_id = await directory(bridge)
        async with accepted_work(bridge) as control:
            identity, prompt = await _released(bridge, control, instance, session_id)
            allowed = bridge.service.repository.dispatchable
            valid, _ = await _forge(bridge, identity, session_id, instance)
            assert await allowed(valid)
            # N1: a marker without the release fact behind it.
            assert not await allowed(
                (
                    await _forge(
                        bridge,
                        identity,
                        session_id,
                        instance,
                        source={
                            "actor_person_id": bridge.source["actor_person_id"],
                            "work_id": identity,
                            "settled_by": "release",
                        },
                    )
                )[0]
            )
            assert not await allowed(
                (
                    await _forge(
                        bridge,
                        identity,
                        session_id,
                        instance,
                        source={
                            "actor_person_id": bridge.source["actor_person_id"],
                            "work_id": identity,
                            "settled_by": "release",
                            "release_operation_id": prompt.id,
                        },
                    )
                )[0]
            )
            assert not await allowed(
                (
                    await _forge(
                        bridge,
                        identity,
                        session_id,
                        instance,
                        source={
                            "actor_person_id": bridge.source["actor_person_id"],
                            "work_id": identity,
                            "answered_via": "release",
                        },
                    )
                )[0]
            )
            # N2: the marker on anything but a session-level deny.
            for action, arguments in (
                ("queue", {"prompt": "more"}),
                ("cancel", {}),
                ("steer", {"prompt": "x"}),
                ("prompt", {"prompt": "x"}),
                ("respond", {"option_id": "allow", "permission_id": "late-bash"}),
                ("respond", {"option_id": "deny", "permission_id": "x", "response": {}}),
                ("respond", {"option_id": "deny", "permission_id": "x", "operation_id": "o"}),
            ):
                forged, _ = await _forge(
                    bridge, identity, session_id, instance, action=action, arguments=arguments
                )
                assert not await allowed(forged), (action, arguments)
            # N3: this record's release named by another Work's row is not its release.
            # (That Work is live, so its ordinary rules apply; the release grants nothing.)
            other, _ = await _forge(
                bridge, identity, session_id, instance, work_id=control.current["id"]
            )
            async with bridge.database.read_session() as reader:
                row = await reader.get(AgentOperationModel, other)
                assert not await release_answer(reader, row)
    finally:
        await computer.close()


# N4: a record that ended without releasing proves nothing.
async def test_dispatchable_refuses_a_marker_on_a_record_that_never_released(bridge):
    computer = await NativeComputer(bridge).start()
    try:
        instance, session_id = await directory(bridge)
        async with accepted_work(bridge) as control:
            identity, operation = await _delegate(bridge, control, instance, session_id)
            await computer.event(operation.id, "completed", {"status": "completed", "text": "a"})
            await settled(bridge)
            forged, _ = await _forge(
                bridge,
                identity,
                session_id,
                instance,
                source={
                    "actor_person_id": bridge.source["actor_person_id"],
                    "work_id": identity,
                    "settled_by": "release",
                    "release_operation_id": operation.id,
                },
            )
            assert not await bridge.service.repository.dispatchable(forged)
    finally:
        await computer.close()


# R1: taken over before the wait arrives: the new holder's card, no release deny.
async def test_a_session_taken_over_is_not_denied_by_the_old_release(bridge):
    computer = await NativeComputer(bridge).start()
    try:
        instance, session_id = await directory(bridge)
        async with accepted_work(bridge) as control:
            await _released(bridge, control, instance, session_id)
            await _delegate(bridge, control, instance, session_id, key="call-takeover")
            await _unowned(computer, "after-takeover")
            await _settle(bridge)
            assert _responds(computer) == []
            cards = [
                t for t in _texts(await _inputs(bridge, control.current["id"])) if "deny_only" in t
            ]
            assert len(cards) == 1
    finally:
        await computer.close()


# R2: taken over after an attempt was registered but before it was written.
async def test_a_registered_release_deny_is_not_written_after_a_takeover(bridge):
    computer = await NativeComputer(bridge).start()
    try:
        instance, session_id = await directory(bridge)
        async with accepted_work(bridge) as control:
            identity, _ = await _released(bridge, control, instance, session_id)
            pending, _ = await _forge(bridge, identity, session_id, instance)
            assert await bridge.service.repository.dispatchable(pending)
            await _delegate(bridge, control, instance, session_id, key="call-takeover")
            assert not await bridge.service.repository.dispatchable(pending)
    finally:
        await computer.close()


# T1: delivery unknown (the socket dropped on the write): the same ID again, once.
async def test_an_unconfirmed_release_deny_is_resent_under_its_original_id(bridge):
    computer = await NativeComputer(bridge).start()
    computer.drop_next = "respond"
    second = None
    try:
        instance, session_id = await directory(bridge)
        async with accepted_work(bridge) as control:
            identity, _ = await _released(bridge, control, instance, session_id)
            await _unowned(computer, "late-bash")
            await settled(bridge)
            [first] = _responds(computer)
            # Maintenance alone never writes it again.
            await _settle(bridge)
            assert len(_responds(computer)) == 1
            # A reconnect (also what a bot restart looks like) writes the same ID once.
            second = NativeComputer(bridge)
            second.hold = {"respond"}
            await second.start()
            await settled(bridge)
            [again] = _responds(second)
            assert again["id"] == first["id"] and again["arguments"] == first["arguments"]
            await second.answer(again["id"], SUBMITTED)
            await _settle(bridge)
            assert len(await _attempts(bridge, identity)) == 1
    finally:
        await computer.close()
        if second is not None:
            await second.close()


# T2: a transient refusal is the record's fact, retried at the next fact (another wait
# in the session, or a reconnect) under a new ID; maintenance ticks never retry it.
async def test_a_transient_refusal_is_retried_at_the_next_fact(bridge):
    computer = await NativeComputer(bridge).start()
    second = None
    try:
        instance, session_id = await directory(bridge)
        async with accepted_work(bridge) as control:
            identity, _ = await _released(bridge, control, instance, session_id)
            before = len(await _inputs(bridge, control.current["id"]))
            computer.fail = {"respond": {"code": "owner_disconnected", "message": "native"}}
            await _unowned(computer, "late-bash")
            await settled(bridge)
            assert len(_responds(computer)) == 1
            for _ in range(3):
                await _settle(bridge)
            assert len(_responds(computer)) == 1
            # Another wait in the session: it is denied, and the refused one is retried.
            computer.fail = {"respond": {"code": "session_not_loaded", "message": "native"}}
            await _unowned(computer, "next-bash")
            await settled(bridge)
            assert sorted(item["arguments"]["permission_id"] for item in _responds(computer)) == [
                "late-bash",
                "late-bash",
                "next-bash",
            ]
            # A reconnect retries both; their answers end the settlement.
            computer.fail = {"respond": {"code": "not_live", "message": "native"}}
            second = NativeComputer(bridge)
            second.hold = {"respond"}
            await second.start()
            await settled(bridge)
            retried = _responds(second)
            assert sorted(item["arguments"]["permission_id"] for item in retried) == [
                "late-bash",
                "next-bash",
            ]
            for item in retried:
                await second.answer(item["id"], SUBMITTED)
            await _settle(bridge)
            ids = [row.id for row in await _attempts(bridge, identity)]
            assert len(ids) == len(set(ids)) == 5
            # Transient failures are the record's facts, not the parent's decisions.
            assert len(await _inputs(bridge, control.current["id"])) == before
    finally:
        await computer.close()
        if second is not None:
            await second.close()


# T3: the device saying the callback is gone, or a native resolution, ends it.
async def test_request_resolved_or_a_native_resolution_ends_the_settlement(bridge):
    computer = await NativeComputer(bridge).start()
    second = None
    try:
        instance, session_id = await directory(bridge)
        async with accepted_work(bridge) as control:
            await _released(bridge, control, instance, session_id)
            computer.fail = {"respond": {"code": "request_resolved", "message": "gone"}}
            await _unowned(computer, "gone-bash")
            await settled(bridge)
            computer.hold = {"respond"}
            await _unowned(computer, "resolved-bash")
            await settled(bridge)
            await _resolved(computer, "resolved-bash")
            await _settle(bridge)
            # Neither a tick nor a reconnect answers either wait again.
            second = await NativeComputer(bridge).start()
            await settled(bridge)
            assert _responds(second) == []
            assert [item["arguments"]["permission_id"] for item in _responds(computer)] == [
                "gone-bash",
                "resolved-bash",
            ]
    finally:
        await computer.close()
        if second is not None:
            await second.close()


# T5: connecting never waits for settlement work.
async def test_a_reconnect_does_not_wait_for_settlement(bridge, monkeypatch):
    computer = await NativeComputer(bridge).start()
    second = None
    stuck = asyncio.Event()

    async def never(*args, **kwargs):
        await stuck.wait()

    try:
        instance, session_id = await directory(bridge)
        async with accepted_work(bridge) as control:
            await _released(bridge, control, instance, session_id)
            monkeypatch.setattr(bridge.service, "_settle_release", never)
            await _unowned(computer, "late-bash")
            await computer.close()
            async with asyncio.timeout(5):
                second = await NativeComputer(bridge).start()
                await directory(bridge)
    finally:
        stuck.set()
        await computer.close()
        if second is not None:
            await second.close()


# R2: taken over between the settlement's check and its registration.
async def test_a_takeover_during_the_settlement_pass_wins(bridge):
    computer = await NativeComputer(bridge).start()
    computer.hold = {"respond"}
    try:
        instance, session_id = await directory(bridge)
        async with accepted_work(bridge) as control:
            identity, _ = await _released(bridge, control, instance, session_id)
            original = bridge.service.repository.unowned_requests
            taker = []

            async def takeover_first(*args):
                if not taker:
                    started = await _start(
                        control, "call-takeover", executor={"agent": instance}, session=session_id
                    )
                    assert started.ok, started
                    taker.append(started.data["id"])
                return await original(*args)

            bridge.service.repository.unowned_requests = takeover_first
            await _unowned(computer, "late-bash")
            await settled(bridge)
            bridge.service.repository.unowned_requests = original
            assert _responds(computer) == []
            async with bridge.database.read_session() as reader:
                assert await last_holder(reader, session_id) == taker[0] != identity
            before = len(_texts(await _inputs(bridge, control.current["id"])))
            await _unowned(computer, "after-takeover")
            await settled(bridge)
            assert _responds(computer) == []
            later = _texts(await _inputs(bridge, control.current["id"]))[before:]
            assert len([text for text in later if "deny_only" in text]) == 1
    finally:
        await computer.close()


# R4: an observation watches but never answers; the release still settles.
async def test_an_observation_after_the_release_does_not_stop_the_settlement(bridge):
    computer = await NativeComputer(bridge).start()
    computer.hold = {"respond"}
    try:
        instance, session_id = await directory(bridge)
        async with accepted_work(bridge) as control:
            await _released(bridge, control, instance, session_id)
            current, control.current = control.current, None
            observed = await _start(
                control,
                "call-observe",
                executor={"agent": instance},
                session=session_id,
                relationship="observe",
                goal="watch",
            )
            assert observed.ok, observed
            control.current = current
            await settled(bridge)
            await _unowned(computer, "after-observe")
            await settled(bridge)
            assert [item["arguments"]["permission_id"] for item in _responds(computer)] == [
                "after-observe"
            ]
    finally:
        await computer.close()


# R5: resuming the released record ends that release; its waits reach the parent.
async def test_a_resume_after_the_release_ends_it(bridge):
    computer = await NativeComputer(bridge).start()
    computer.hold = {"respond"}
    try:
        instance, session_id = await directory(bridge)
        async with accepted_work(bridge) as control:
            identity, operation = await _delegate(bridge, control, instance, session_id)
            await computer.event(operation.id, "completed", {"status": "completed", "text": "a"})
            await settled(bridge)
            released = await control.tool_outcome(
                "manage_task", {"action": "release", "id": identity}, "release-completed"
            )
            assert released.ok, released
            await settled(bridge)
            resumed = await control.tool_outcome(
                "manage_task", {"action": "resume", "id": identity, "text": "more"}, "resume"
            )
            assert resumed.ok, resumed
            await settled(bridge)
            before = len(_texts(await _inputs(bridge, control.current["id"])))
            await _unowned(computer, "while-resumed")
            await settled(bridge)
            assert _responds(computer) == []
            later = _texts(await _inputs(bridge, control.current["id"]))[before:]
            assert len([text for text in later if "deny_only" in text]) == 1
    finally:
        await computer.close()


# T1: a resend that cannot be written records nothing; the next reconnect writes it.
async def test_an_unwritable_resend_is_not_recorded_as_not_executed(bridge, monkeypatch):
    computer = await NativeComputer(bridge).start()
    computer.drop_next = "respond"
    second = third = None
    try:
        instance, session_id = await directory(bridge)
        async with accepted_work(bridge) as control:
            identity, _ = await _released(bridge, control, instance, session_id)
            await _unowned(computer, "late-bash")
            await settled(bridge)
            [first] = _responds(computer)
            original = bridge.service.repository.dispatchable

            async def refused(identity):
                return False

            monkeypatch.setattr(bridge.service.repository, "dispatchable", refused)
            second = await NativeComputer(bridge).start()
            await settled(bridge)
            assert _responds(second) == []
            [attempt] = await _attempts(bridge, identity)
            assert attempt.result_json is None
            monkeypatch.setattr(bridge.service.repository, "dispatchable", original)
            third = NativeComputer(bridge)
            third.hold = {"respond"}
            await third.start()
            await settled(bridge)
            [again] = _responds(third)
            assert again["id"] == first["id"]
    finally:
        await computer.close()
        for other in (second, third):
            if other is not None:
                await other.close()


# T4: only a wait in the released session itself retries its refused answers.
async def test_a_wait_in_another_session_does_not_retry_this_release(bridge):
    computer = await NativeComputer(bridge).start()
    try:
        instance, session_id = await directory(bridge)
        async with accepted_work(bridge) as control:
            await _released(bridge, control, instance, session_id)
            computer.fail = {"respond": {"code": "owner_disconnected", "message": "native"}}
            await _unowned(computer, "late-bash")
            await settled(bridge)
            assert len(_responds(computer)) == 1
            released_session = computer.native_session
            computer.native_session = released_session + "-second"
            await _unowned(computer, "elsewhere-bash")
            await settled(bridge)
            computer.native_session = released_session
            assert len(_responds(computer)) == 1
    finally:
        await computer.close()
