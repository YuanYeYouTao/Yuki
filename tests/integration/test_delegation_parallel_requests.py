"""A parent never waits on a delegation that is waiting on the parent (2026-11-20).

Production 06:41 UTC: two parallel permission cards reached the parent in one model
request; it answered one, then registered ``wait(run)`` and both sides waited three
hours. Failure cases written before the implementation (taskbook
``Yuki-委派权限卡并行处理与等待死锁任务书-2026-11-20`` §4):
- W1: with one of two parallel cards still unanswered, ``wait(run)`` registers and
  blocks (the incident); or the refusal depends on ``mode`` any/all, a deadline or the
  other conditions; or a wait that does not target the run is refused;
- W2: with every card answered, ``wait(run)`` is still refused, or the registered wait
  is not woken when the delegation completes;
- W3: a card already consumed (but unanswered) no longer blocks the wait;
- W4: a card arriving between the check and the registration is lost;
- W5: a card arriving while the parent waits does not wake it;
- W6: an unowned deny-only wait on the record does not block the wait, or stays
  listed after the parent denied it;
- W7: an observation's waits (handled at the native terminal) block the parent;
- W8: an answer does not list the record's other unanswered requests, lists the one
  just answered while its receipt is in flight, or counts an answer whose receipt is
  unknown or failed (which must leave the request pending and answerable again);
- W9: ``get_tasks(id, cursor)`` cannot page a delegation's timeline
  (``invalid_work_progress_cursor``);
- W10: with the real connector, two ``can_use_tool`` in one assistant message are not
  answered one by one (``test_delegation_parallel_requests_e2e.py``).
"""

import json

from sqlalchemy import select, update
from tests.integration.test_external_agent_bridge import (
    NativeComputer,
    accepted_work,
    directory,
    settled,
)
from tests.integration.test_external_agent_bridge import bridge as bridge
from tests.integration.test_external_agent_delegation import (
    _delegate,
    _inputs,
    _row,
    _set_state,
)
from tests.integration.test_external_agent_review_fixes import _unowned

from yuki.external_agents.delegation import pending_requests, waiting_on_parent
from yuki.runtime.work_schema_v1 import inputs
from yuki.runtime.work_wait import WorkWaitRepository
from yuki.runtime.work_wait_schema import waits

SUBMITTED = {
    "status": "response_submitted",
    "response_submitted": True,
    "pending": False,
    "admitted": True,
}


async def _cards(bridge, parent):
    cards = []
    for row in await _inputs(bridge, parent):
        text = json.loads(row["payload_json"]).get("text", "")
        if text.startswith("{") and '"permission_id"' in text:
            cards.append(json.loads(text))
    return cards


async def _requests(bridge, computer, control, instance, session_id, count=2):
    identity, operation = await _delegate(bridge, control, instance, session_id)
    await _set_state(bridge, control.current["id"], "running")
    for index in range(count):
        await computer.event(
            operation.id, "permission", {"native_request_id": f"toolu-{index}", "tool": "Bash"}
        )
    cards = await _cards(bridge, control.current["id"])
    assert len(cards) == count, cards
    return identity, operation, [card["permission_id"] for card in cards]


async def _answer(bridge, computer, control, identity, permission_id, key, result=SUBMITTED):
    """Answer one card; the receipt is held so ``remaining`` is read while in flight."""
    outcome = await control.tool_outcome(
        "message_task",
        {"id": identity, "reply_to": permission_id, "answer": "allow", "text": "ok"},
        key,
    )
    assert outcome.ok, outcome
    await settled(bridge)
    [command] = [
        item
        for item in computer.commands
        if item["action"] == "respond" and item["id"] not in computer.answered_ids
    ]
    computer.answered_ids.add(command["id"])
    await computer.answer(command["id"], result)
    await settled(bridge)
    return outcome


async def _active_waits(bridge, parent):
    async with bridge.database.read_session() as reader:
        return list(
            (
                await reader.execute(
                    select(waits.c.id).where(waits.c.work_id == parent, waits.c.status == "active")
                )
            ).scalars()
        )


def _computer(bridge):
    computer = NativeComputer(bridge)
    computer.hold = {"respond"}
    computer.answered_ids = set()
    return computer


def _ids(items):
    return [item["permission_id"] for item in items]


# W1 (the incident), W2
async def test_w1_w2_wait_on_a_run_that_waits_for_you_is_refused_with_each_request(bridge):
    computer = await _computer(bridge).start()
    try:
        instance, session_id = await directory(bridge)
        async with accepted_work(bridge) as control:
            identity, operation, (first, second) = await _requests(
                bridge, computer, control, instance, session_id
            )
            await _answer(bridge, computer, control, identity, second, "answer-2")
            refused = await control.tool_outcome("wait", {"for": [{"run": identity}]}, "wait-1")
            assert not refused.ok and refused.error_code == "run_waiting_for_you"
            assert f"permission_id={first}（" in refused.public_message
            assert f"permission_id={second}（" not in refused.public_message
            assert await _active_waits(bridge, control.current["id"]) == []
            # W2: once every request is answered the wait registers and completion wakes it.
            await _answer(bridge, computer, control, identity, first, "answer-1")
            assert (await _row(bridge, identity))["state"] == "waiting_external"
            waiting = await control.tool_outcome("wait", {"for": [{"run": identity}]}, "wait-2")
            assert waiting.ok, waiting
            assert len(await _active_waits(bridge, control.current["id"])) == 1
            await computer.event(operation.id, "completed", {"status": "completed", "text": "ok"})
            assert (await _row(bridge, identity))["state"] == "completed"
            assert await WorkWaitRepository(control.repository).deliver_due() == 1
            assert await _active_waits(bridge, control.current["id"]) == []
    finally:
        await computer.close()


# W1: any / all / deadline mixtures are all checked; a wait not on the run is not.
async def test_w1_refusal_is_independent_of_mode_and_other_conditions(bridge):
    computer = await _computer(bridge).start()
    try:
        instance, session_id = await directory(bridge)
        async with accepted_work(bridge) as control:
            identity, _, _ = await _requests(bridge, computer, control, instance, session_id)
            for key, args in (
                ("any", {"for": [{"run": identity}, {"message": True}]}),
                ("all", {"for": [{"run": identity}, {"message": True}], "all": True}),
                ("deadline", {"for": [{"run": identity}], "deadline": "2099-01-01T00:00:00Z"}),
            ):
                refused = await control.tool_outcome("wait", args, f"wait-{key}")
                assert not refused.ok and refused.error_code == "run_waiting_for_you", key
            assert await _active_waits(bridge, control.current["id"]) == []
            unrelated = await control.tool_outcome("wait", {"for": [{"message": True}]}, "wait-m")
            assert unrelated.ok, unrelated
    finally:
        await computer.close()


# W3: consumed cards are still unanswered facts.
async def test_w3_consumed_cards_still_block_the_wait(bridge):
    computer = await _computer(bridge).start()
    try:
        instance, session_id = await directory(bridge)
        async with accepted_work(bridge) as control:
            identity, _, _ = await _requests(bridge, computer, control, instance, session_id)
            async with bridge.database.immediate_session() as writer:
                await writer.execute(
                    update(inputs)
                    .where(inputs.c.work_id == control.current["id"])
                    .values(state="consumed")
                )
            assert not await control.has_pending_business_inputs()
            refused = await control.tool_outcome("wait", {"for": [{"run": identity}]}, "wait-c")
            assert not refused.ok and refused.error_code == "run_waiting_for_you"
    finally:
        await computer.close()


# W4: a card landing between the check and the registration reaches the next step.
async def test_w4_a_card_racing_the_registration_is_not_lost(bridge, monkeypatch):
    computer = await _computer(bridge).start()
    try:
        instance, session_id = await directory(bridge)
        async with accepted_work(bridge) as control:
            identity, operation = await _delegate(bridge, control, instance, session_id)
            await _set_state(bridge, control.current["id"], "running")
            original = WorkWaitRepository.register

            async def racing(self, *args, **kwargs):
                await computer.event(
                    operation.id, "permission", {"native_request_id": "toolu-race", "tool": "Bash"}
                )
                return await original(self, *args, **kwargs)

            monkeypatch.setattr(WorkWaitRepository, "register", racing)
            waited = await control.tool_outcome("wait", {"for": [{"run": identity}]}, "wait-r")
            monkeypatch.setattr(WorkWaitRepository, "register", original)
            assert waited.ok, waited
            pending = [
                row
                for row in await _inputs(bridge, control.current["id"])
                if row["state"] == "pending" and row["source_key"].endswith(":request:toolu-race")
            ]
            assert len(pending) == 1
            # The activation does not end on its wait: the card is the next model input.
            assert await control.has_pending_business_inputs()
            again = await control.tool_outcome("wait", {"for": [{"run": identity}]}, "wait-r2")
            assert not again.ok and again.error_code == "run_waiting_for_you"
    finally:
        await computer.close()


# W5: a card arriving while the parent waits wakes it (regression of current behavior).
async def test_w5_a_card_arriving_during_the_wait_wakes_the_parent(bridge):
    computer = await _computer(bridge).start()
    try:
        instance, session_id = await directory(bridge)
        async with accepted_work(bridge) as control:
            identity, operation = await _delegate(bridge, control, instance, session_id)
            waited = await control.tool_outcome("wait", {"for": [{"run": identity}]}, "wait-w")
            assert waited.ok, waited
            await _set_state(bridge, control.current["id"], "waiting_external")
            await computer.event(
                operation.id, "permission", {"native_request_id": "toolu-late", "tool": "Bash"}
            )
            assert (await _row(bridge, control.current["id"]))["state"] == "queued"
            assert await _active_waits(bridge, control.current["id"]) == []
            assert len(await _cards(bridge, control.current["id"])) == 1
    finally:
        await computer.close()


# W6: an unowned deny-only wait on the record is still the parent's to deny.
async def test_w6_a_deny_only_wait_blocks_the_wait_until_denied(bridge):
    computer = await _computer(bridge).start()
    try:
        instance, session_id = await directory(bridge)
        async with accepted_work(bridge) as control:
            identity, _ = await _delegate(bridge, control, instance, session_id)
            await _set_state(bridge, control.current["id"], "running")
            await _unowned(computer, "toolu-self-started")
            await settled(bridge)
            refused = await control.tool_outcome("wait", {"for": [{"run": identity}]}, "wait-u")
            assert not refused.ok and refused.error_code == "run_waiting_for_you"
            assert "只能 deny" in refused.public_message
            async with bridge.database.read_session() as reader:
                [waiting] = await waiting_on_parent(reader, identity)
            assert waiting["answer_at"] == "deny_only"
            denied = await control.tool_outcome(
                "message_task",
                {"id": identity, "reply_to": waiting["permission_id"], "answer": "deny"},
                "deny-u",
            )
            assert denied.ok, denied
            assert denied.data["remaining"] == []
            waited = await control.tool_outcome("wait", {"for": [{"run": identity}]}, "wait-u2")
            assert waited.ok, waited
    finally:
        await computer.close()


# W7: an observation's waits are handled at the native terminal, never by Yuki.
async def test_w7_an_observation_never_waits_on_the_parent(bridge):
    computer = await _computer(bridge).start()
    try:
        instance, session_id = await directory(bridge)
        async with accepted_work(bridge) as control:
            observed = await control.tool_outcome(
                "start_subtask",
                {
                    "goal": "watch it",
                    "executor": {"agent": instance},
                    "session": session_id,
                    "relationship": "observe",
                },
                "observe",
            )
            assert observed.ok, observed
            await settled(bridge)
            identity = observed.data["id"]
            [operation] = [item for item in computer.commands if item["action"] == "watch"]
            await computer.event(
                operation["id"], "permission", {"native_request_id": "toolu-obs", "tool": "Bash"}
            )
            async with bridge.database.read_session() as reader:
                [request] = await pending_requests(reader, identity, "observe")
                assert request["answer_at"] == "native_terminal"
                assert await waiting_on_parent(reader, identity) == []
    finally:
        await computer.close()


# W8: each answer lists exactly the record's other unanswered requests.
async def test_w8_each_answer_lists_the_remaining_requests(bridge):
    computer = await _computer(bridge).start()
    try:
        instance, session_id = await directory(bridge)
        async with accepted_work(bridge) as control:
            identity, _, (first, second, third) = await _requests(
                bridge, computer, control, instance, session_id, count=3
            )
            # Read while the first answer's receipt is still in flight.
            answered = await _answer(bridge, computer, control, identity, first, "answer-a")
            assert answered.data["answered"] == first
            assert _ids(answered.data["remaining"]) == [second, third]
            answered = await _answer(bridge, computer, control, identity, third, "answer-c")
            assert _ids(answered.data["remaining"]) == [second]
            answered = await _answer(bridge, computer, control, identity, second, "answer-b")
            assert answered.data["remaining"] == []
    finally:
        await computer.close()


# W8: unknown or failed receipts are not answers; the request can be answered again.
async def test_w8_unknown_or_failed_answers_leave_the_request_pending(bridge):
    computer = await _computer(bridge).start()
    try:
        instance, session_id = await directory(bridge)
        async with accepted_work(bridge) as control:
            identity, _, (first, second) = await _requests(
                bridge, computer, control, instance, session_id
            )
            await _answer(
                bridge,
                computer,
                control,
                identity,
                first,
                "unknown-1",
                {"status": "unknown", "reason": "native_result_not_confirmed"},
            )
            await _answer(
                bridge,
                computer,
                control,
                identity,
                second,
                "failed-2",
                {"error": {"code": "request_resolved", "message": "gone"}, "pending": False},
            )
            refused = await control.tool_outcome("wait", {"for": [{"run": identity}]}, "wait-k")
            assert not refused.ok and refused.error_code == "run_waiting_for_you"
            assert f"permission_id={first}（" in refused.public_message
            assert f"permission_id={second}（" in refused.public_message
            again = await _answer(bridge, computer, control, identity, first, "retry-1")
            assert _ids(again.data["remaining"]) == [second]
    finally:
        await computer.close()


# W9: the timeline's own cursor pages the timeline.
async def test_w9_get_tasks_pages_a_delegation_timeline_with_its_cursor(bridge):
    computer = await _computer(bridge).start()
    try:
        instance, session_id = await directory(bridge)
        async with accepted_work(bridge) as control:
            identity, operation = await _delegate(bridge, control, instance, session_id)
            for text in ("one", "two", "three"):
                await computer.event(operation.id, "progress", {"text": text})
            first = await control.tool_outcome("get_tasks", {"id": identity, "limit": 2}, "page-1")
            assert first.ok, first
            cursor = first.data["timeline"]["next_cursor"]
            assert cursor is not None
            second = await control.tool_outcome(
                "get_tasks", {"id": identity, "limit": 2, "cursor": str(cursor)}, "page-2"
            )
            assert second.ok, second
            texts = [event["payload"].get("text") for event in second.data["timeline"]["events"]]
            assert "three" in texts and "one" not in texts
    finally:
        await computer.close()
