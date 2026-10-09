"""Completion summaries do not duplicate the original Work checkpoint."""

import json
import time
from types import SimpleNamespace

import pytest
from jsonschema import validate as validate_schema
from sqlalchemy import event, select, update
from tests.support.runtime_work_helpers import _persisted_tool_receipt
from tests.support.subagents_helpers import stack

from qq_ai_bot.runtime.subagent_schema import children
from qq_ai_bot.runtime.subagent_tools import execute_subagent, subagent_tools
from qq_ai_bot.runtime.work_budget_schema import budgets
from qq_ai_bot.runtime.work_control import WorkControl
from qq_ai_bot.runtime.work_repository import WorkConflict
from qq_ai_bot.runtime.work_schema_v1 import effects, work


async def _completed_child(
    database, tmp_path, *, result="Verified result: /workspace/tasks/report.md"
):
    repo, workers, parent_lease, parent, identity = await stack(database, tmp_path)
    lease = await workers.acquire(identity)
    row = await repo.get(identity)

    async def validate():
        assert await repo.valid(lease)

    control = WorkControl(repo, lease, row["source_key"], json.loads(row["source_json"]), validate)
    control.current = row
    payload = {"version": 1, "facts": [{"text": "x" * 270000, "refs": ["goal"]}]}
    await control.update_context_note(payload, "large-note")
    row = await repo.get(identity)
    checkpoint = json.loads(row["checkpoint_json"])
    assert len(row["checkpoint_json"].encode()) > 256 * 1024
    await repo.accept_control(
        lease,
        identity,
        {
            "action": "complete",
            "call_key": "done",
            "result": result,
        },
    )
    row = await repo.get(identity)
    await repo.transition(lease, identity, row["revision"], "completed", reason="done")
    await workers.finish(lease)
    # The committed checkpoint, including its published result, is what the parent reads.
    checkpoint = json.loads((await repo.get(identity))["checkpoint_json"])
    parent_control = SimpleNamespace(repository=repo, lease=parent_lease, current=parent, source={})
    return repo, workers, parent_control, lease, identity, checkpoint


async def test_child_result_and_parent_notice_preserve_full_unicode_result(database, tmp_path):
    text = "完整研究結果" * 4000
    repo, workers, parent, lease, identity, _ = await _completed_child(
        database, tmp_path, result=text
    )
    row = await workers.related(parent.current["id"], identity, include_checkpoint=True)
    assert json.loads(row["result_json"])["text"] == text
    notice = json.loads(
        (await repo.pending(parent.lease, work_id=parent.current["id"]))[0]["payload_json"]
    )
    assert json.loads(notice["text"])["text"] == text
    await repo.release(lease)
    await repo.release(parent.lease)


async def test_large_note_completion_notifies_and_result_reads_original_checkpoint(
    database, tmp_path
):
    repo, workers, parent, lease, identity, checkpoint = await _completed_child(database, tmp_path)
    async with database.sessions() as reader:
        saved = await reader.scalar(
            select(children.c.result_json).where(children.c.work_id == identity)
        )
    assert len(saved.encode()) < 1024
    assert "checkpoint" not in json.loads(saved)
    pending = await repo.pending(parent.lease, work_id=parent.current["id"])
    assert len(pending) == 1
    assert identity in pending[0]["payload_json"]
    assert "/workspace/tasks/report.md" in pending[0]["payload_json"]
    result = await execute_subagent(
        parent, "subagent_control", {"action": "result", "child_id": identity}, "read-result"
    )
    child = result["children"][0]
    assert child["child_id"] == identity and child["state"] == "completed"
    assert child["result"]["checkpoint"] == checkpoint
    assert (
        child["result"]["checkpoint"]["context_note"]["payload"]["facts"][0]["text"] == "x" * 270000
    )
    assert child["result"]["text"] == "Verified result: /workspace/tasks/report.md"
    statements = []

    def capture(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)

    event.listen(database.engine.sync_engine, "before_cursor_execute", capture)
    try:
        for action in ("status", "list"):
            summary = await execute_subagent(
                parent, "subagent_control", {"action": action, "child_id": identity}, action
            )
            assert "checkpoint" not in summary["children"][0]["result"]
    finally:
        event.remove(database.engine.sync_engine, "before_cursor_execute", capture)
    assert all("checkpoint_json" not in statement for statement in statements)
    await workers.finish(lease)
    assert len(await repo.pending(parent.lease, work_id=parent.current["id"])) == 1
    with pytest.raises(ValueError, match="subagent_not_owned"):
        await workers.related("unrelated-root", identity, include_checkpoint=True)
    await repo.release(lease)
    await repo.release(parent.lease)


async def test_resumed_child_checkpoint_is_not_presented_as_previous_result(database, tmp_path):
    repo, workers, parent, lease, identity, _checkpoint = await _completed_child(database, tmp_path)
    await repo.release(lease)
    await workers.message(parent.lease, parent.current["id"], identity, "resume", "Check again.")
    result = await workers.related(parent.current["id"], identity, include_checkpoint=True)
    receipt = json.loads(result["result_json"])
    assert receipt["state"] == "completed"
    assert result["state"] == "queued"
    assert "checkpoint" not in receipt
    assert receipt["checkpoint_status"] == "unavailable_for_result_revision"
    assert receipt["text"] == "Verified result: /workspace/tasks/report.md"
    await repo.release(parent.lease)


async def test_archived_legacy_result_retains_its_original_checkpoint(database, tmp_path):
    repo, workers, parent, lease, identity, _checkpoint = await _completed_child(database, tmp_path)
    await repo.release(lease)
    snapshot = {"original": "legacy completed checkpoint"}
    async with database.immediate_session() as writer:
        saved = json.loads(
            await writer.scalar(
                select(children.c.result_json).where(children.c.work_id == identity)
            )
        )
        saved["checkpoint"] = snapshot
        await writer.execute(
            update(children)
            .where(children.c.work_id == identity)
            .values(archived_at=time.time(), result_json=json.dumps(saved))
        )
        await writer.execute(update(work).where(work.c.id == identity).values(checkpoint_json="{}"))
    result = await workers.related(parent.current["id"], identity, include_checkpoint=True)
    assert json.loads(result["result_json"])["checkpoint"] == snapshot
    await repo.release(parent.lease)


async def test_stale_child_owner_cannot_replace_completed_result(database, tmp_path):
    repo, workers, parent, lease, identity, _checkpoint = await _completed_child(database, tmp_path)
    await repo.release(lease)
    with pytest.raises(WorkConflict):
        await workers.finish(lease)
    result = await workers.related(parent.current["id"], identity, include_checkpoint=True)
    assert (
        json.loads(result["result_json"])["text"] == "Verified result: /workspace/tasks/report.md"
    )
    await repo.release(parent.lease)


async def test_children_queue_without_admission_quota_and_keep_execution_concurrency(
    database, tmp_path
):
    database.subagent_concurrency = 1
    database.subagents_enabled = True
    repo, workers, lease, parent, first = await stack(database, tmp_path)
    parent_control = SimpleNamespace(repository=repo, lease=lease, current=parent, source={})
    brief = {
        "goal": "详细子任务要求；" * 1200,
        "context": "完整背景资料；" * 4000,
        "acceptance": "完整验收要求；" * 1200,
        "files": [f"/workspace/input-{index}.txt" for index in range(33)],
        "output_kind": "answer",
    }
    validate_schema(
        brief, next(item.parameters for item in subagent_tools() if item.name == "subagent_start")
    )
    identities = [first]
    for index in range(9):
        accepted = await execute_subagent(
            parent_control, "subagent_start", brief, f"queued-{index}"
        )
        identities.append(accepted["child_id"])
    assert len(set(identities)) == 10
    saved = await workers.related(parent["id"], identities[1])
    assert len(saved["brief_json"].encode()) > 65536
    assert json.loads(saved["brief_json"]) == brief
    replayed = await workers.start(
        lease,
        parent["id"],
        "queued-0",
        brief,
    )
    assert replayed == identities[1]
    first_lease = await workers.acquire(first)
    assert first_lease is not None
    text = "完整补充指令；" * 1200
    await workers.message(lease, parent["id"], first, "detailed-message", text)
    pending = await repo.pending(first_lease, work_id=first)
    assert json.loads(json.loads(pending[0]["payload_json"])["text"])["text"] == text
    assert await workers.acquire(identities[1]) is None
    assert (await repo.get(identities[1]))["state"] == "queued"
    await repo.release(first_lease)
    next_lease = await workers.acquire(identities[1])
    assert next_lease is not None
    await repo.release(next_lease)
    await repo.release(lease)


async def test_verified_mutation_child_completes_without_extra_result_prose(database, tmp_path):
    repo, workers, parent_lease, parent, initial = await stack(database, tmp_path)
    await workers.cancel(parent_lease, parent["id"], initial)
    identity = await workers.start(
        parent_lease,
        parent["id"],
        "mutation-child",
        {"goal": "apply verified edit", "output_kind": "state_change"},
    )
    lease = await workers.acquire(identity)
    row = await repo.get(identity)

    async def validate():
        assert await repo.valid(lease)

    control = WorkControl(
        repo, lease, row["source_key"], json.loads(row["source_json"]), validate, current=row
    )
    refused = json.loads(await control.execute("task_control", {"action": "complete"}, "too-early"))
    assert refused["error"] == "work_completion_requires_execution_evidence"
    await _persisted_tool_receipt(
        control,
        "verified-edit",
        "workspace_edit",
        json.dumps({"ok": True, "mutation_committed": True, "data": {"status": "succeeded"}}),
    )
    result = json.loads(await control.execute("task_control", {"action": "complete"}, "done"))
    assert result["ok"]
    await control.settle(pending_inputs=False)
    await workers.finish(lease)
    delivered = await workers.related(parent["id"], identity, include_checkpoint=True)
    receipt = json.loads(delivered["result_json"])
    assert delivered["state"] == receipt["state"] == "completed"
    assert receipt["text"] == receipt["checkpoint"]["sync_result"] == ""
    assert any(fact["mutation_committed"] is True for fact in await control.effect_evidence())
    pending = await repo.pending(parent_lease, work_id=parent["id"])
    signal = json.loads(json.loads(pending[0]["payload_json"])["text"])
    assert signal["child_id"] == identity and signal["state"] == "completed"
    assert "subagent_control.result" in signal["detail"]


async def test_retained_completed_tree_reopens_original_budget_after_eight_days_with_paused_root(
    database, tmp_path
):
    repo, workers, parent, child_lease, identity, _ = await _completed_child(database, tmp_path)
    await repo.release(child_lease)
    with pytest.raises(ValueError, match="resume_instruction_required"):
        await execute_subagent(
            parent, "subagent_control", {"action": "resume", "child_id": identity}, "no-directive"
        )
    assert (await repo.get(identity))["state"] == "completed"
    pending = await repo.pending(parent.lease, work_id=parent.current["id"])
    await repo.stage(parent.lease, [item["id"] for item in pending], "read-original-result")
    await repo.consume(parent.lease, "read-original-result")
    await repo.checkpoint(parent.lease, parent.current["id"], {}, models=3, tools=4)
    root = await repo.get(parent.current["id"])
    await repo.accept_control(
        parent.lease, root["id"], {"action": "complete", "call_key": "root-done", "result": "done"}
    )
    await repo.transition(parent.lease, root["id"], root["revision"], "completed")
    other = await repo.accept(
        parent.lease, source_key="other-root", source={}, goal="keep paused", output_kind="answer"
    )
    await repo.transition(parent.lease, other["id"], other["revision"], "suspended")
    async with database.immediate_session() as session:
        await session.execute(
            update(work).where(work.c.id == root["id"]).values(updated=time.time() - 8 * 86400)
        )
    parent.current = None
    parent.requests_started = 2
    resumed = await execute_subagent(
        parent,
        "subagent_control",
        {"action": "resume", "child_id": identity, "instruction": "Verify the new requirements."},
        "new-directive",
    )
    assert resumed["children"][0]["child_id"] == identity
    assert resumed["children"][0]["state"] == "queued"
    reopened = await repo.get(root["id"])
    assert parent.current["id"] == root["id"] and reopened["state"] == "running"
    assert reopened["model_requests"] == 5 and reopened["tool_calls"] == 4
    assert (await repo.get(other["id"]))["state"] == "suspended"
    async with database.sessions() as session:
        budget = (
            (await session.execute(select(budgets).where(budgets.c.root_id == root["id"])))
            .mappings()
            .one()
        )
    assert budget["models"] == 5 and budget["tools"] == 4
    child = await workers.related(root["id"], identity)
    assert child["root_id"] == root["id"] and child["archived_at"] is None
    async with database.immediate_session() as session:
        await session.execute(
            update(children).where(children.c.work_id == identity).values(archived_at=time.time())
        )
    with pytest.raises(ValueError, match="subagent_archived_or_unknown"):
        await workers.reopen_parent(parent.lease, identity, models=0)
    await repo.release(parent.lease)


@pytest.mark.parametrize("state", ["suspended", "waiting_user"])
async def test_paused_child_resumes_original_goal_without_a_new_instruction(
    database, tmp_path, state
):
    repo, workers, parent_lease, parent, identity = await stack(database, tmp_path)
    lease = await workers.acquire(identity)
    await repo.checkpoint(lease, identity, {"sync_result": "retained evidence"}, models=2, tools=3)
    child = await repo.get(identity)
    await repo.transition(lease, identity, child["revision"], state)
    await repo.release(lease)
    before = await repo.get(identity)
    control = SimpleNamespace(repository=repo, lease=parent_lease, current=parent, source={})
    result = await execute_subagent(
        control, "subagent_control", {"action": "resume", "child_id": identity}, "continue-original"
    )
    assert result["children"][0]["state"] == "queued"
    after = await repo.get(identity)
    assert after["goal"] == before["goal"] == "draw"
    assert after["checkpoint_json"] == before["checkpoint_json"]
    assert (after["model_requests"], after["tool_calls"]) == (2, 3)
    lease = await workers.acquire(identity)
    assert lease is not None and lease.work_id == identity
    assert not await repo.pending(lease, work_id=identity)
    await repo.release(lease)
    await repo.release(parent_lease)


@pytest.mark.parametrize("cancel_kind", ["parent_cancelled", "parent_obsolete"])
async def test_child_cancellation_retires_accepted_control_and_retains_real_receipts(
    database, tmp_path, cancel_kind
):
    repo, workers, parent_lease, parent, identity = await stack(database, tmp_path)
    lease = await workers.acquire(identity)
    row = await repo.get(identity)

    async def validate():
        assert await repo.valid(lease)

    control = WorkControl(
        repo, lease, row["source_key"], json.loads(row["source_json"]), validate, current=row
    )
    await _persisted_tool_receipt(
        control, "real-edit", "workspace_edit", json.dumps({"ok": True, "mutation_committed": True})
    )
    await repo.checkpoint(
        lease, identity, {"sync_result": "verified before cancellation"}, models=2, tools=3
    )
    await repo.accept_control(
        lease,
        identity,
        {"action": "complete", "call_key": "old-complete", "result": "not committed"},
    )
    async with database.sessions() as session:
        before = [
            dict(row)
            for row in (
                await session.execute(select(effects).where(effects.c.work_id == identity))
            ).mappings()
        ]
    if cancel_kind == "parent_cancelled":
        await workers.cancel(parent_lease, parent["id"], identity)
        assert not await repo.valid(lease)
    else:
        await repo.release(lease)
        await repo.transition(parent_lease, parent["id"], parent["revision"], "cancelled")
        assert await workers.acquire(identity) is None
    cancelled = await repo.get(identity)
    checkpoint = json.loads(cancelled["checkpoint_json"])
    assert cancelled["state"] == "cancelled" and cancelled["reason"] == cancel_kind
    assert "accepted_control" not in checkpoint
    assert checkpoint["sync_result"] == "verified before cancellation"
    assert (cancelled["model_requests"], cancelled["tool_calls"]) == (2, 3)
    async with database.sessions() as session:
        after = [
            dict(row)
            for row in (
                await session.execute(select(effects).where(effects.c.work_id == identity))
            ).mappings()
        ]
    assert after == before and before[0]["state"] == "accepted"
    await repo.release(parent_lease)
