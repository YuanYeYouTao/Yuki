"""Completion summaries do not duplicate the original Work checkpoint."""

import json
import time
from types import SimpleNamespace

import pytest
from sqlalchemy import event, select, update
from tests.unit.test_subagents import stack

from qq_ai_bot.runtime.subagent_schema import children
from qq_ai_bot.runtime.subagent_tools import execute_subagent
from qq_ai_bot.runtime.work_control import WorkControl
from qq_ai_bot.runtime.work_repository import WorkConflict
from qq_ai_bot.runtime.work_schema_v1 import work


async def _completed_child(database, tmp_path):
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
            "result": "Verified result: /workspace/tasks/report.md",
        },
    )
    row = await repo.get(identity)
    await repo.transition(lease, identity, row["revision"], "completed", reason="done")
    await workers.finish(lease)
    # The committed checkpoint, including its published result, is what the parent reads.
    checkpoint = json.loads((await repo.get(identity))["checkpoint_json"])
    parent_control = SimpleNamespace(repository=repo, lease=parent_lease, current=parent, source={})
    return repo, workers, parent_control, lease, identity, checkpoint


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
