"""Original worker trees survive ordinary completion and consume real questions."""

import asyncio
import json
from dataclasses import replace

import pytest
from sqlalchemy import select
from tests.support.work_runner_helpers import case, run, tool

from yuki.domain.messages import ChatResponse
from yuki.runtime.subagent_repository import SubagentRepository
from yuki.runtime.subagent_schema import children
from yuki.runtime.subagent_tools import manage_subtask, message_task, start_subtask
from yuki.runtime.work_budget_schema import budgets
from yuki.runtime.work_control import WorkControl
from yuki.runtime.work_repository import WorkConflict
from yuki.runtime.work_schema_v1 import inputs, work
from yuki.runtime.work_wait import WorkWaitRepository


async def worker_case(database, tmp_path, calls, *, close_parent=False, wait_parent=False):
    probe = await case(database, tmp_path, [])
    database.subagents_enabled = True
    parent = probe.control
    workers = SubagentRepository(probe.repository)
    identity = await workers.start(
        parent.lease, parent.current["id"], "original-worker", {"goal": "original child goal"}
    )
    if close_parent:
        await parent.execute("manage_task", {"action": "finish"}, "parent-complete")
        await parent.settle(pending_inputs=False)
        assert parent.current["state"] == "completed"
    if wait_parent:
        waited = json.loads(await parent.execute("wait", {"for": [{"run": identity}]}, "wait"))
        assert waited.get("ok") is not False, waited
        await parent.settle(pending_inputs=False)
        assert parent.current["state"] == "waiting_external"
    lease = await workers.acquire(identity)
    row = await probe.repository.get(identity)

    async def validate():
        assert await probe.repository.valid(lease)

    control = WorkControl(
        probe.repository,
        lease,
        row["source_key"],
        json.loads(row["source_json"]),
        validate,
        current=row,
    )
    probe.control = control
    probe.runtime = replace(
        probe.runtime, work_control=control, conversation_key=f"worker:{identity}"
    )
    scripted = iter([ChatResponse("", 0, tool_calls=calls)])
    probe.provider._responder = lambda _: next(scripted)
    return probe, parent, workers, identity


def receipts(control):
    return {
        message.tool_call_id: json.loads(message.content)
        for message in control.session.transcript.request().messages
        if message.role == "tool"
    }


async def test_child_source_and_message_body_are_encoded_before_writer(
    database, tmp_path, monkeypatch
):
    import yuki.runtime.subagent_repository as child_module
    import yuki.runtime.work_repository as work_module

    original_child_encode, original_work_encode = child_module.encode_json, work_module.encode_json
    prepared = []

    def check_source(value):
        if isinstance(value, dict) and "principal_kind" in value:
            assert not database._writer.locked()
            prepared.append("source")
        return original_work_encode(value)

    def check_child(value):
        if isinstance(value, dict) and ("principal_kind" in value or "text" in value):
            assert not database._writer.locked()
            prepared.append("child")
        return original_child_encode(value)

    monkeypatch.setattr(work_module, "encode_json", check_source)
    monkeypatch.setattr(child_module, "encode_json", check_child)
    probe, parent, workers, identity = await worker_case(database, tmp_path, ())
    body = "original child question " * 1000
    message = await workers.message(
        probe.control.lease,
        parent.current["id"],
        identity,
        "original-question",
        body,
        ask=True,
        to_parent=True,
    )
    assert message["waiting_parent"] is True
    async with database.read_session() as reader:
        raw = await reader.scalar(
            select(inputs.c.payload_json).where(inputs.c.source_key == "original-question")
        )
    payload = json.loads(raw)
    assert payload["child_id"] == identity
    assert json.loads(payload["text"])["text"] == body
    assert "source" in prepared and "child" in prepared


async def test_work_control_keeps_an_actual_unknown_commit_ack(database, tmp_path, monkeypatch):
    from sqlalchemy.ext.asyncio import AsyncSession

    from yuki.runtime.effect_outcomes import ResultCapture, current_result_capture

    probe = await case(database, tmp_path, [])
    original_commit = AsyncSession.commit
    commits = 0

    async def lose_ack(session):
        nonlocal commits
        await original_commit(session)
        commits += 1
        raise ValueError("original Work update commit acknowledgement lost")

    capture = ResultCapture(probe.control.current["id"], "original-update")
    token = current_result_capture.set(capture)
    try:
        with monkeypatch.context() as fault:
            fault.setattr(AsyncSession, "commit", lose_ack)
            result = json.loads(
                await probe.control.execute(
                    "manage_task",
                    {"action": "update", "goal": "actual committed goal"},
                    "original-update",
                )
            )
    finally:
        current_result_capture.reset(token)
    assert result["ok"] is False and result["status"] == "unknown"
    assert result["error"] == "database_failure"
    assert capture.outcome.uncertain and capture.outcome.mutation_committed is None
    assert commits == 1
    stored = await probe.repository.get(probe.control.current["id"])
    assert stored["goal"] == "actual committed goal"


async def test_worker_admission_switch_covers_both_routes_and_keeps_accepted_child(
    database, tmp_path
):
    probe, parent, workers, identity = await worker_case(database, tmp_path, ())
    database.subagents_enabled = False
    direct = json.loads(
        await probe.control.execute("start_subtask", {"goal": "new child"}, "disabled-start")
    )
    assert direct["ok"] is False and direct["error"] == "subagent_admission_disabled"
    async with database.read_session() as reader:
        assert list(await reader.scalars(select(children.c.work_id))) == [identity]
    # The original accepted call keeps its identity after new admission closes.
    replayed = await workers.start(
        parent.lease, parent.current["id"], "original-worker", {"goal": "original child goal"}
    )
    assert replayed == identity
    await probe.repository.release(probe.control.lease)
    lease = await workers.acquire(identity)
    assert lease is not None
    row = await probe.repository.get(identity)

    async def validate():
        assert await probe.repository.valid(lease)

    resumed = WorkControl(
        probe.repository,
        lease,
        row["source_key"],
        json.loads(row["source_json"]),
        validate,
        current=row,
    )
    completed = json.loads(
        await resumed.execute(
            "manage_task", {"action": "finish", "result": "original result"}, "complete"
        )
    )
    assert completed.get("ok") is not False, completed
    await resumed.settle(pending_inputs=False)
    await workers.finish(lease)
    result = await workers.related(parent.current["id"], identity, include_checkpoint=True)
    assert json.loads(result["result_json"])["text"] == "original result"
    assert len(await parent.pending()) == 1
    await probe.repository.release(lease)
    await probe.repository.release(parent.lease)


async def test_parent_complete_still_dispatches_original_child(database, tmp_path):
    database.subagents_enabled = True
    calls = (
        tool("manage_task", {"action": "finish"}, "complete-parent"),
        tool("start_subtask", {"goal": "dispatch original child"}, "spawn-child"),
    )
    probe = await case(database, tmp_path, [ChatResponse("", 0, tool_calls=calls)])
    await run(probe)
    spawned = receipts(probe.control)["spawn-child"]
    assert spawned.get("ok") is not False, spawned
    await probe.control.settle(pending_inputs=False)
    assert spawned["status"] == "running"
    child = await probe.repository.get(spawned["id"])
    assert child["state"] == "queued" and child["parent_work_id"] == probe.control.current["id"]
    assert probe.control.current["state"] == "completed"
    lease = await SubagentRepository(probe.repository).acquire(child["id"])
    assert lease is not None
    await probe.repository.release(lease)
    await probe.repository.release(probe.control.lease)


async def test_worker_complete_keeps_mail_delivery_grandchild_and_finish_receipt(
    database, tmp_path
):
    calls = (
        tool("manage_task", {"action": "finish", "result": "actual child result"}, "complete"),
        tool("message_task", {"text": "detailed parent report"}, "report"),
        tool("start_subtask", {"goal": "independent nested step"}, "spawn-grandchild"),
        tool("send", {"text": "group deliverable"}, "delivery"),
    )
    probe, parent, workers, identity = await worker_case(database, tmp_path, calls)
    await run(probe)
    paired = receipts(probe.control)
    assert all(paired[call.id].get("ok") is not False for call in calls)
    assert probe.observed == ["send"]
    nested = await probe.repository.get(paired["spawn-grandchild"]["id"])
    assert nested["parent_work_id"] == identity and nested["state"] == "queued"
    await probe.control.settle(pending_inputs=False)
    await workers.finish(probe.control.lease)
    await workers.finish(probe.control.lease)
    assert (await probe.repository.get(identity))["state"] == "completed"
    mail = await parent.pending()
    assert len(mail) == 2
    assert any(item["source_key"].endswith("report") for item in mail)
    assert any(
        item["source_key"] == f"worker-result:{identity}:{probe.control.current['revision']}"
        for item in mail
    )
    result = await workers.related(parent.current["id"], identity, include_checkpoint=True)
    assert json.loads(result["result_json"])["text"] == "actual child result"
    assert not await parent.has_pending_business_inputs()
    await probe.repository.release(probe.control.lease)
    await probe.repository.release(parent.lease)


async def test_child_question_keeps_its_remaining_calls_and_retires_parent_completion(
    database, tmp_path
):
    calls = (
        tool("message_task", {"text": "which artifact?", "ask": True}, "question"),
        tool("write_fixture", {}, "save"),
        tool("start_subtask", {"goal": "independent nested step"}, "spawn"),
    )
    probe, parent, workers, identity = await worker_case(database, tmp_path, calls)
    result = await run(probe)
    assert result.work_state == "waiting_user"
    assert all(receipts(probe.control)[call.id].get("ok") is not False for call in calls)
    assert probe.observed == ["write_fixture"]
    assert await parent.has_pending_business_inputs()
    await probe.control.settle(pending_inputs=False)
    await workers.finish(probe.control.lease)
    await probe.repository.release(probe.control.lease)
    await parent.execute("manage_task", {"action": "finish"}, "unobserved-complete")
    await parent.settle(pending_inputs=True)
    assert (await probe.repository.get(parent.current["id"]))["state"] == "queued"
    assert (await probe.repository.get(identity))["state"] == "waiting_user"
    assert "accepted_control" not in json.loads(parent.current["checkpoint_json"])
    await probe.repository.release(parent.lease)


async def test_question_to_completed_parent_is_saved_without_pausing_child(database, tmp_path):
    calls = (
        tool("message_task", {"text": "which artifact?", "ask": True}, "question"),
        tool("write_fixture", {}, "save"),
        tool("manage_task", {"action": "finish", "result": "available work done"}, "complete"),
    )
    probe, parent, workers, identity = await worker_case(
        database, tmp_path, calls, close_parent=True
    )
    parent_before = await probe.repository.get(parent.current["id"])
    await run(probe)
    paired = receipts(probe.control)
    assert paired["question"].get("ok") is not False and "status" not in paired["question"]
    assert probe.observed == ["write_fixture"]
    await probe.control.settle(pending_inputs=False)
    await workers.finish(probe.control.lease)
    assert (await probe.repository.get(identity))["state"] == "completed"
    assert await probe.repository.get(parent.current["id"]) == parent_before
    assert any(item["source_key"].endswith("question") for item in await parent.pending())
    await probe.repository.release(probe.control.lease)
    await probe.repository.release(parent.lease)


@pytest.mark.parametrize("close_parent", [False, True])
async def test_complete_then_question_accepts_only_a_consumable_parent_wait(
    database, tmp_path, close_parent
):
    calls = (
        tool("manage_task", {"action": "finish", "result": "earlier result"}, "complete"),
        tool("message_task", {"text": "need your choice", "ask": True}, "ask-after-complete"),
        tool("write_fixture", {}, "save-after-question"),
    )
    probe, parent, workers, identity = await worker_case(
        database, tmp_path, calls, close_parent=close_parent
    )
    await run(probe)
    paired = receipts(probe.control)
    assert (paired["ask-after-complete"].get("status") == "waiting_parent") is not close_parent
    assert paired["save-after-question"].get("ok") is not False
    assert probe.observed == ["write_fixture"]
    assert probe.control.accepted["action"] == ("complete" if close_parent else "need_input")
    await probe.control.settle(pending_inputs=False)
    await workers.finish(probe.control.lease)
    assert (await probe.repository.get(identity))["state"] == (
        "completed" if close_parent else "waiting_user"
    )
    await probe.repository.release(probe.control.lease)
    await probe.repository.release(parent.lease)


@pytest.mark.parametrize("restart", [False, True])
async def test_saved_child_question_wait_survives_activation_failure_or_restart(
    database, tmp_path, restart
):
    probe, parent, workers, identity = await worker_case(database, tmp_path, ())
    receipt = await message_task(
        probe.control, {"text": "need your choice", "ask": True}, "question"
    )
    assert receipt["status"] == "waiting_parent"
    original = await probe.repository.get(identity)
    accepted = json.loads(original["checkpoint_json"])["accepted_control"]
    assert accepted == {
        "action": "need_input",
        "call_key": "question",
        "reason": "need your choice",
    }
    control = probe.control
    if restart:
        await probe.repository.release(control.lease)
        lease = await workers.acquire(identity)

        async def validate():
            assert await probe.repository.valid(lease)

        control = WorkControl(
            probe.repository,
            lease,
            original["source_key"],
            json.loads(original["source_json"]),
            validate,
            current=await probe.repository.get(identity),
        )
    await control.recover_failure(RuntimeError("lost activation after saved question receipt"))
    row = await probe.repository.get(identity)
    assert row["state"] == "waiting_user"
    assert json.loads(row["checkpoint_json"])["reason"] == "need your choice"
    assert row["model_requests"] == original["model_requests"] == 0
    mail = await parent.pending()
    assert len(mail) == 1 and mail[0]["source_key"] == "question"
    await probe.repository.release(control.lease)
    await probe.repository.release(parent.lease)


async def test_child_question_wakes_parent_from_owned_wait_then_answer_resumes_original_child(
    database, tmp_path
):
    calls = (tool("message_task", {"text": "which result?", "ask": True}, "question"),)
    probe, parent, workers, identity = await worker_case(
        database, tmp_path, calls, wait_parent=True
    )
    await run(probe)
    await probe.control.settle(pending_inputs=False)
    await workers.finish(probe.control.lease)
    await probe.repository.release(probe.control.lease)
    waiting = await WorkWaitRepository(probe.repository).describe(parent.current["id"])
    assert waiting["status"] == "cancelled"
    assert (await probe.repository.get(parent.current["id"]))["state"] == "queued"
    assert (await probe.repository.get(identity))["state"] == "waiting_user"
    assert await parent.has_pending_business_inputs()
    await parent.take_inputs("answer-original-question")
    await parent.confirm_inputs()
    answer = json.loads(
        await parent.execute(
            "message_task", {"id": identity, "text": "publish the first result"}, "answer"
        )
    )
    assert answer.get("ok") is not False and "status" not in answer
    row = await probe.repository.get(identity)
    assert row["state"] == "queued" and row["parent_work_id"] == parent.current["id"]
    resumed = await workers.acquire(identity)
    assert resumed is not None and resumed.work_id == identity
    assert len(await probe.repository.pending(resumed)) == 1
    await probe.repository.release(resumed)
    await probe.repository.release(parent.lease)


@pytest.mark.parametrize("unrelated", [False, True])
async def test_foreground_manages_original_child_without_accepting_another_work(
    database, tmp_path, unrelated
):
    probe, parent, workers, identity = await worker_case(database, tmp_path, (), close_parent=True)
    await probe.repository.checkpoint(
        probe.control.lease, identity, {"kept": "checkpoint"}, tools=2
    )
    row = await probe.repository.get(identity)
    await probe.repository.transition(
        probe.control.lease, identity, row["revision"], "waiting_user"
    )
    await probe.repository.release(probe.control.lease)
    original_parent = await probe.repository.get(parent.current["id"])
    unrelated_work = None
    if unrelated:
        unrelated_work = await probe.repository.accept(
            parent.lease,
            source_key="unrelated",
            source=parent.source,
            goal="unrelated current goal",
        )
    parent.current = unrelated_work
    parent.settled = False
    parent.ending = None
    before = await probe.repository.get(identity)
    async with database.read_session() as reader:
        budget_before = dict(
            (
                await reader.execute(
                    select(budgets).where(budgets.c.root_id == original_parent["id"])
                )
            )
            .mappings()
            .one()
        )
        original_work_ids = set(await reader.scalars(select(work.c.id)))
    listed = json.loads(
        await parent.execute("get_tasks", {"parent": original_parent["id"]}, "list")
    )
    assert [task["id"] for task in listed["tasks"]] == [identity]
    viewed = json.loads(await parent.execute("get_tasks", {"id": identity}, "status"))
    assert viewed["id"] == identity and viewed["status"] == "waiting_parent"
    await manage_subtask(parent, "resume", identity, "resume", None)
    await message_task(parent, {"id": identity, "text": "continue original task"}, "update")
    resumed = await workers.acquire(identity)
    assert resumed is not None
    with pytest.raises(WorkConflict, match="work_activation_obsolete"):
        await message_task(probe.control, {"text": "obsolete lease"}, "obsolete")
    await manage_subtask(parent, "cancel", identity, "cancel-original", None)
    assert not await probe.repository.valid(resumed)
    after = await probe.repository.get(identity)
    assert after["state"] == "cancelled"
    assert (after["source_json"], after["goal"], after["tool_calls"]) == (
        before["source_json"],
        before["goal"],
        before["tool_calls"],
    )
    assert await probe.repository.get(original_parent["id"]) == original_parent
    if unrelated_work:
        assert await probe.repository.get(unrelated_work["id"]) == unrelated_work
    async with database.read_session() as reader:
        budget_after = dict(
            (
                await reader.execute(
                    select(budgets).where(budgets.c.root_id == original_parent["id"])
                )
            )
            .mappings()
            .one()
        )
        assert set(await reader.scalars(select(work.c.id))) == original_work_ids
    assert budget_after == budget_before
    await probe.repository.release(parent.lease)


@pytest.mark.parametrize("action", ["get", "cancel", "resume", "message"])
async def test_foreground_subagent_tools_retain_real_actor_scope(database, tmp_path, action):
    probe, parent, _workers, identity = await worker_case(database, tmp_path, (), close_parent=True)
    await probe.repository.release(probe.control.lease)
    before = await probe.repository.get(identity)
    parent.current = None
    parent.source = {**parent.source, "actor_person_id": "different-actual-person"}
    if action == "get":
        # The directory row is visible; private progress and the child result are not.
        result = json.loads(await parent.execute("get_tasks", {"id": identity}, "get"))
        assert result["progress_available"] is False and "result" not in result
    elif action == "message":
        with pytest.raises(ValueError, match="subtask_not_owned"):
            await message_task(parent, {"id": identity, "text": "borrowed"}, action)
    else:
        with pytest.raises(ValueError, match="subtask_not_owned"):
            await manage_subtask(parent, action, identity, action, "borrowed")
    assert await probe.repository.get(identity) == before
    await probe.repository.release(parent.lease)


async def test_question_does_not_resume_an_independently_suspended_parent(database, tmp_path):
    probe, parent, _workers, _identity = await worker_case(database, tmp_path, ())
    current = await probe.repository.get(parent.current["id"])
    suspended = await probe.repository.transition(
        parent.lease, current["id"], current["revision"], "suspended", reason="context_capacity"
    )
    response = await message_task(probe.control, {"text": "need your choice", "ask": True}, "ask")
    assert response["status"] == "waiting_parent"
    assert await probe.repository.get(parent.current["id"]) == suspended
    assert await parent.has_pending_business_inputs()
    await probe.repository.release(probe.control.lease)
    await probe.repository.release(parent.lease)


async def test_new_child_still_requires_an_actual_work_owner(database, tmp_path):
    probe = await case(database, tmp_path, [])
    database.subagents_enabled = True
    probe.control.current = None
    with pytest.raises(ValueError, match="subtask_requires_current_task"):
        await start_subtask(probe.control, {"goal": "no owner"}, "start")
    async with database.read_session() as reader:
        assert list(await reader.scalars(select(children.c.work_id))) == []
    await probe.repository.release(probe.control.lease)


@pytest.mark.parametrize("question_first", [False, True])
async def test_question_and_completion_writer_order_preserves_truth(
    database, tmp_path, question_first
):
    probe, parent, workers, identity = await worker_case(database, tmp_path, ())
    await parent.execute("manage_task", {"action": "finish"}, "parent-complete")
    # Release the active worker lease so the parent is not merely draining an
    # active child; the question owns the continuation in this race.
    await probe.repository.release(probe.control.lease)
    allowed = asyncio.Event()
    finished = asyncio.Event()

    async def question():
        if not question_first:
            await finished.wait()
        lease = await workers.acquire(identity)
        row = await probe.repository.get(identity)

        async def validate():
            assert await probe.repository.valid(lease)

        control = WorkControl(
            probe.repository,
            lease,
            row["source_key"],
            json.loads(row["source_json"]),
            validate,
            current=row,
        )
        receipt = json.loads(
            await control.execute("message_task", {"text": "need decision", "ask": True}, "ask")
        )
        allowed.set()
        await probe.repository.release(lease)
        return receipt

    async def complete():
        if question_first:
            await allowed.wait()
        await parent.settle(pending_inputs=False)
        finished.set()

    receipt, _ = await asyncio.gather(question(), complete())
    assert receipt.get("ok") is not False
    assert (receipt.get("status") == "waiting_parent") is question_first
    row = await probe.repository.get(parent.current["id"])
    assert row["state"] == ("queued" if question_first else "completed")
    async with database.read_session() as reader:
        entries = list(
            await reader.scalars(select(inputs.c.id).where(inputs.c.work_id == row["id"]))
        )
        assert len(entries) == 1
    await probe.repository.release(parent.lease)
