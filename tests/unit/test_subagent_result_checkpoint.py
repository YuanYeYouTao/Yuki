"""Completion summaries do not duplicate the original Work checkpoint."""

import json
import time
from types import SimpleNamespace

import pytest
from jsonschema import validate as validate_schema
from sqlalchemy import select, update
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


async def test_large_note_completion_notifies_and_result_reads_original_checkpoint(
    database, tmp_path
):
    text = "Verified result: /workspace/tasks/report.md\n" + "完整研究結果" * 4000
    repo, workers, parent, lease, identity, checkpoint = await _completed_child(
        database, tmp_path, result=text
    )
    async with database.sessions() as reader:
        saved = await reader.scalar(
            select(children.c.result_json).where(children.c.work_id == identity)
        )
    assert "checkpoint" not in json.loads(saved)
    pending = await repo.pending(parent.lease, work_id=parent.current["id"])
    assert len(pending) == 1
    assert identity in pending[0]["payload_json"]
    assert json.loads(json.loads(pending[0]["payload_json"])["text"])["text"] == text
    result = await execute_subagent(
        parent, "subagent_control", {"action": "result", "child_id": identity}, "read-result"
    )
    child = result["children"][0]
    assert child["child_id"] == identity and child["state"] == "completed"
    assert child["result"]["checkpoint"] == checkpoint
    assert (
        child["result"]["checkpoint"]["context_note"]["payload"]["facts"][0]["text"] == "x" * 270000
    )
    assert child["result"]["text"] == text
    for action in ("status", "list"):
        summary = await execute_subagent(
            parent, "subagent_control", {"action": action, "child_id": identity}, action
        )
        assert "checkpoint" not in summary["children"][0]["result"]
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


@pytest.mark.parametrize("repair_barrier", ["obsolete_generation", "execution_capacity"])
async def test_verified_mutation_child_completes_without_extra_result_prose(
    database, tmp_path, repair_barrier
):
    database.subagent_concurrency = 1
    repo, workers, parent_lease, parent, initial = await stack(database, tmp_path)
    obsolete = []
    if repair_barrier == "obsolete_generation":
        from qq_ai_bot.conversation.hydrate import bump_canonical_generation
        from qq_ai_bot.persistence.models import ChatEventModel

        obsolete = [initial]
        for index in range(7):
            obsolete.append(
                await workers.start(
                    parent_lease, parent["id"], f"old-{index}", {"goal": "original goal"}
                )
            )
        for identity in obsolete:
            old_lease = await workers.acquire(identity)
            row = await repo.get(identity)

            async def validate_old(lease=old_lease):
                assert await repo.valid(lease)

            old_control = WorkControl(
                repo,
                old_lease,
                row["source_key"],
                json.loads(row["source_json"]),
                validate_old,
                current=row,
            )
            await old_control.complete_final("original result", "original-final")
            await old_control.settle(pending_inputs=False)
            await repo.release(old_lease)
        await repo.release(parent_lease)
        async with database.sessions() as reader:
            event_id = await reader.scalar(select(ChatEventModel.id))
        async with database.immediate_session() as writer:
            generation = await bump_canonical_generation(
                writer, parent["conversation_id"], event_id=event_id
            )
        parent_lease = await repo.acquire(parent["conversation_id"], generation)
        parent = await repo.accept(
            parent_lease, source_key="current-parent", source={}, goal="apply verified edit"
        )
        assert await workers.acquire(obsolete[0], reconcile=True) is None
    else:
        await workers.cancel(parent_lease, parent["id"], initial)
    identity = await workers.start(
        parent_lease,
        parent["id"],
        "mutation-child",
        {"goal": "apply verified edit"},
    )
    lease = await workers.acquire(identity)
    row = await repo.get(identity)

    async def validate():
        assert await repo.valid(lease)

    control = WorkControl(
        repo, lease, row["source_key"], json.loads(row["source_json"]), validate, current=row
    )
    await _persisted_tool_receipt(
        control,
        "verified-edit",
        "workspace_edit",
        json.dumps({"ok": True, "mutation_committed": True, "data": {"status": "succeeded"}}),
    )
    await repo.checkpoint(lease, identity, None, tools=1)
    result = json.loads(await control.execute("task_control", {"action": "complete"}, "done"))
    assert result["ok"]
    await control.settle(pending_inputs=False)
    assert any(fact["mutation_committed"] is True for fact in await control.effect_evidence())
    original = await repo.get(identity)
    await repo.release(lease)
    active_lease = None
    if repair_barrier == "execution_capacity":
        unrelated = await repo.accept(
            parent_lease, source_key="unrelated-parent", source={}, goal="unrelated work"
        )
        active = await workers.start(
            parent_lease, unrelated["id"], "active-worker", {"goal": "unrelated active work"}
        )
        active_lease = await workers.acquire(active)
        assert active_lease is not None
        queued = await workers.start(
            parent_lease, unrelated["id"], "queued-worker", {"goal": "unrelated queued work"}
        )
        assert await workers.acquire(queued) is None
    async with database.sessions() as reader:
        original_budget = dict(
            (await reader.execute(select(budgets).where(budgets.c.root_id == parent["id"])))
            .mappings()
            .one()
        )
        old_rows = [
            dict(row)
            for row in (
                await reader.execute(select(work).where(work.c.id.in_(obsolete)))
            ).mappings()
        ]
    await workers.maintain()
    delivered = await workers.related(parent["id"], identity, include_checkpoint=True)
    receipt = json.loads(delivered["result_json"])
    assert delivered["state"] == receipt.get("state") == "completed"
    assert receipt["text"] == receipt["checkpoint"]["sync_result"] == ""
    pending = await repo.pending(parent_lease, work_id=parent["id"])
    signal = next(
        value
        for item in pending
        if (value := json.loads(json.loads(item["payload_json"])["text"]))["child_id"] == identity
    )
    assert signal["child_id"] == identity and signal["state"] == "completed"
    assert "subagent_control.result" in signal["detail"]
    assert await repo.get(identity) == original
    async with database.sessions() as reader:
        assert (
            dict(
                (await reader.execute(select(budgets).where(budgets.c.root_id == parent["id"])))
                .mappings()
                .one()
            )
            == original_budget
        )
        assert [
            dict(row)
            for row in (
                await reader.execute(select(work).where(work.c.id.in_(obsolete)))
            ).mappings()
        ] == old_rows
    await workers.maintain()
    assert (
        sum(
            json.loads(json.loads(item["payload_json"])["text"])["child_id"] == identity
            for item in await repo.pending(parent_lease, work_id=parent["id"])
        )
        == 1
    )
    if active_lease is not None:
        assert await repo.valid(active_lease)
        assert await workers.acquire(queued) is None
        await repo.release(active_lease)
    await repo.release(parent_lease)


async def test_retained_child_resumes_without_reopening_ancestors_or_resetting_budget(
    database, tmp_path
):
    repo, workers, parent, child_lease, identity, _ = await _completed_child(database, tmp_path)
    await repo.release(child_lease)
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
    other = await repo.accept(parent.lease, source_key="other-root", source={}, goal="keep paused")
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
        {"action": "resume", "child_id": identity},
        "new-directive",
    )
    assert resumed["children"][0]["child_id"] == identity
    assert resumed["children"][0]["state"] == "queued"
    reopened = await repo.get(root["id"])
    assert parent.current is None and reopened["state"] == "completed"
    assert reopened["model_requests"] == 3 and reopened["tool_calls"] == 4
    assert (await repo.get(other["id"]))["state"] == "suspended"
    async with database.sessions() as session:
        budget = (
            (await session.execute(select(budgets).where(budgets.c.root_id == root["id"])))
            .mappings()
            .one()
        )
    assert budget["models"] == 3 and budget["tools"] == 4
    child = await workers.related(root["id"], identity)
    assert child["parent_work_id"] == root["id"] and child["archived_at"] is None
    async with database.immediate_session() as session:
        await session.execute(
            update(children).where(children.c.work_id == identity).values(archived_at=time.time())
        )
    with pytest.raises(ValueError, match="subagent_archived"):
        await execute_subagent(
            parent, "subagent_control", {"action": "resume", "child_id": identity}, "archived"
        )
    await repo.release(parent.lease)


@pytest.mark.parametrize("state", ["suspended", "waiting_user", "failed"])
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


async def test_three_level_creation_queries_budget_and_direct_parent_recovery(database, tmp_path):
    from qq_ai_bot.persistence.control_work_query import ControlWorkQueryAdapter
    from qq_ai_bot.runtime.work_tree import ancestor_work_ids, descendant_work_ids

    database.subagents_enabled = True
    repo, workers, parent_lease, root, sibling = await stack(database, tmp_path)

    async def validate():
        assert await repo.valid(parent_lease)

    control = WorkControl(repo, parent_lease, root["source_key"], {}, validate, current=root)
    derived = json.loads(
        await control.execute(
            "task_control", {"action": "derive", "goal": "verify a branch"}, "derive"
        )
    )
    assert derived["ok"], derived
    branch_id = derived["derived_work_id"]
    replayed = json.loads(
        await control.execute(
            "task_control", {"action": "derive", "goal": "verify a branch"}, "derive"
        )
    )
    assert replayed["derived_work_id"] == branch_id
    branch = await repo.get(branch_id)
    assert branch["parent_work_id"] == root["id"]
    assert json.loads(branch["source_json"]) == json.loads(root["source_json"])
    parent = SimpleNamespace(repository=repo, lease=parent_lease, current=branch, source={})
    leaf = await execute_subagent(
        parent, "subagent_start", {"goal": "check the actual output"}, "leaf"
    )
    leaf_id = leaf["child_id"]
    queried = json.loads(
        await control.execute("task_control", {"action": "get", "work_id": leaf_id}, "get-leaf")
    )["work"]
    assert queried["parent_work_id"] == branch_id
    assert queried["ancestor_work_ids"] == [branch_id, root["id"]]
    assert queried["budget_root_id"] == root["id"]
    directory = json.loads(
        await control.execute(
            "task_control", {"action": "list", "parent_work_id": branch_id}, "list-branch"
        )
    )
    assert [item["work_id"] for item in directory["works"]] == [leaf_id]
    async with database.sessions() as session:
        assert await ancestor_work_ids(session, leaf_id) == [branch_id, root["id"]]
        assert set(await descendant_work_ids(session, root["id"])) == {branch_id, leaf_id, sibling}
    leaf_lease = await workers.acquire(leaf_id)
    await repo.checkpoint(leaf_lease, leaf_id, {"retained": "original result"}, models=2, tools=3)

    async def validate_leaf():
        assert await repo.valid(leaf_lease)

    leaf_row = await repo.get(leaf_id)
    worker_control = WorkControl(
        repo,
        leaf_lease,
        leaf_row["source_key"],
        json.loads(leaf_row["source_json"]),
        validate_leaf,
        current=leaf_row,
    )
    nested = json.loads(
        await worker_control.execute(
            "task_control", {"action": "derive", "goal": "check a nested result"}, "nested"
        )
    )
    assert nested["ok"], nested
    nested_id = nested["derived_work_id"]
    nested_row = await repo.get(nested_id)
    nested_read = json.loads(
        await worker_control.execute(
            "task_control", {"action": "get", "work_id": nested_id}, "read-nested"
        )
    )["work"]
    assert nested_read["parent_work_id"] == leaf_id
    assert nested_read["ancestor_work_ids"] == [leaf_id, branch_id, root["id"]]
    assert nested_read["budget_root_id"] == root["id"]
    listed = json.loads(
        await worker_control.execute(
            "task_control", {"action": "list", "parent_work_id": leaf_id}, "list-nested"
        )
    )
    assert [item["work_id"] for item in listed["works"]] == [nested_id]
    for outside in (root["id"], branch_id, sibling):
        denied = json.loads(
            await worker_control.execute(
                "task_control", {"action": "get", "work_id": outside}, f"read-outside:{outside}"
            )
        )
        assert not denied["ok"] and denied["error"] == "work_not_found_or_not_authorized"
        denied_stop = json.loads(
            await worker_control.execute(
                "task_control",
                {"action": "cancel", "work_id": outside},
                f"stop-outside:{outside}",
            )
        )
        assert not denied_stop["ok"]
    for action, state in (("fail", "failed"), ("resume", "queued"), ("cancel", "cancelled")):
        managed = json.loads(
            await worker_control.execute(
                "task_control", {"action": action, "work_id": nested_id}, f"{action}-nested"
            )
        )
        assert managed["ok"] and managed["work_id"] == nested_id and managed["state"] == state
        assert worker_control.current["id"] == leaf_id and worker_control.ending is None
        current_nested = await repo.get(nested_id)
        assert current_nested["source_json"] == nested_row["source_json"]
        assert current_nested["goal"] == nested_row["goal"]
        assert current_nested["model_requests"] == nested_row["model_requests"] == 0
        assert (await repo.get(leaf_id))["state"] == "running"
        assert (await repo.get(sibling))["state"] == "queued"
        assert (await repo.get(root["id"]))["state"] == "running"
    async with database.immediate_session() as session:
        await session.execute(
            update(children).where(children.c.work_id == nested_id).values(archived_at=time.time())
        )
    archived = json.loads(
        await worker_control.execute(
            "task_control", {"action": "get", "work_id": nested_id}, "read-archived"
        )
    )
    assert archived["work"]["state"] == "cancelled"
    details = await ControlWorkQueryAdapter(database.sessions).read_work(leaf_id)
    assert details.fields["parent_work_id"] == branch_id
    assert details.fields["root_id"] == details.fields["budget_root_id"] == root["id"]
    assert details.fields["shared_budget"]["models"] == 2
    assert details.fields["shared_budget"]["tools"] == 3
    await repo.transition(parent_lease, branch_id, branch["revision"], "waiting_external")
    await repo.accept_control(
        leaf_lease, leaf_id, {"action": "complete", "call_key": "leaf-done", "result": "checked"}
    )
    row = await repo.get(leaf_id)
    await repo.transition(leaf_lease, leaf_id, row["revision"], "completed")
    # A restart after settlement, before finish, publishes the same result once.
    await repo.release(leaf_lease)
    await workers.maintain()
    await workers.maintain()
    notice = await repo.pending(parent_lease, work_id=branch_id)
    assert len(notice) == 1
    assert json.loads(json.loads(notice[0]["payload_json"])["text"])["child_id"] == leaf_id
    assert (await repo.get(branch_id))["state"] == "queued"
    assert (await repo.get(root["id"]))["state"] == "running"
    assert not await repo.pending(parent_lease, work_id=root["id"])
    # Cancelling this branch preserves the root and the separate sibling.
    await workers.resume(parent_lease, branch_id, leaf_id, key="continue-leaf")
    active_leaf = await workers.acquire(leaf_id)
    assert active_leaf is not None
    stopped = json.loads(
        await control.execute(
            "task_control", {"action": "cancel", "work_id": branch_id}, "stop-branch"
        )
    )
    assert stopped["ok"], stopped
    assert (await repo.get(branch_id))["state"] == "cancelled"
    assert (await repo.get(leaf_id))["state"] == "cancelled"
    assert not await repo.valid(active_leaf)
    assert (await repo.get(sibling))["state"] == "queued"
    assert (await repo.get(root["id"]))["state"] == "running"
    await repo.release(parent_lease)


async def test_derived_call_retrieves_terminal_child_without_new_identity_or_budget(
    database, tmp_path
):
    repo, _workers, lease, root, _sibling = await stack(database, tmp_path)
    child = await repo.derive(lease, root["id"], "original-derive", "original child goal")
    await repo.checkpoint(lease, child["id"], {}, models=2, tools=3)
    await repo.accept_control(
        lease,
        child["id"],
        {"action": "complete", "call_key": "finish", "result": "original result"},
    )
    before = await repo.transition(lease, child["id"], child["revision"], "completed")
    async with database.sessions() as session:
        budget_before = (
            (await session.execute(select(budgets).where(budgets.c.root_id == root["id"])))
            .mappings()
            .one()
        )
    replay = await repo.derive(lease, root["id"], "original-derive", "original child goal")
    assert replay == before
    assert replay["state"] == "completed"
    assert json.loads(replay["checkpoint_json"])["sync_result"] == "original result"
    async with database.sessions() as session:
        assert (
            (await session.execute(select(budgets).where(budgets.c.root_id == root["id"])))
            .mappings()
            .one()
        ) == budget_before
        assert len(list(await session.scalars(select(work.c.id)))) == 3
    await repo.release(lease)


async def test_privacy_purge_removes_one_actual_tree_and_preserves_other_scope(database, tmp_path):
    from qq_ai_bot.conversation.hydrate import ensure_canonical_conversation
    from qq_ai_bot.identity.canonical_repository import ensure_space
    from qq_ai_bot.runtime.work_schema_v1 import scope

    repo, workers, lease, root, sibling = await stack(database, tmp_path)
    branch = await repo.derive(lease, root["id"], "private-branch", "private child")
    leaf = await workers.start(lease, branch["id"], "private-leaf", {"goal": "private evidence"})
    await repo.prepare_effect(lease, root["id"], "private-receipt", "tool")
    await repo.record_effect("private-receipt", "unknown", {"outcome": {"uncertain": True}})
    async with database.immediate_session() as session:
        space_id = await ensure_space(session, "20002")
        other_conversation = await ensure_canonical_conversation(
            session, kind="space", primary_scope_key="bot:80001:group:20002", space_id=space_id
        )
    other_lease = await repo.acquire(other_conversation.conversation_id, 1)
    other_root = await repo.accept(
        other_lease, source_key="other-tree", source={}, goal="keep this"
    )
    other_child = await workers.start(
        other_lease, other_root["id"], "other-worker", {"goal": "retained original evidence"}
    )
    await repo.checkpoint(
        other_lease, other_root["id"], {"retained": "other scope"}, models=3, tools=4
    )
    await repo.prepare_effect(other_lease, other_root["id"], "other-receipt", "tool")
    await repo.record_effect("other-receipt", "unknown", {"outcome": {"uncertain": True}})
    tables = (work, children, budgets, scope, effects)
    async with database.sessions() as session:
        before = {
            table.name: list((await session.execute(select(table))).mappings()) for table in tables
        }
    removed_ids = {root["id"], branch["id"], sibling, leaf}
    async with database.immediate_session() as session:
        await repo.purge_scope(session, lease.conversation_id)
    assert not await repo.valid(lease)
    for identity in removed_ids:
        assert await repo.get(identity) is None
    async with database.sessions() as session:
        after = {
            table.name: list((await session.execute(select(table))).mappings()) for table in tables
        }
        assert [row["id"] for row in after[work.name]] == [other_root["id"], other_child]
        assert after[work.name] == [
            row for row in before[work.name] if row["id"] not in removed_ids
        ]
        assert after[children.name] == [
            row for row in before[children.name] if row["work_id"] not in removed_ids
        ]
        assert after[budgets.name] == [
            row for row in before[budgets.name] if row["root_id"] not in removed_ids
        ]
        assert after[scope.name] == [
            row for row in before[scope.name] if row["conversation_id"] != lease.conversation_id
        ]
        assert after[effects.name] == [
            row for row in before[effects.name] if row["work_id"] not in removed_ids
        ]
    assert (await repo.get(other_child))["parent_work_id"] == other_root["id"]
    assert await repo.valid(other_lease)
    await repo.release(other_lease)


async def test_original_sandbox_completion_keeps_remaining_all_wait_conditions(database, tmp_path):
    from datetime import UTC, datetime
    from uuid import uuid4

    from tests.unit.test_work_settlement_writer import _running_work

    from qq_ai_bot.runtime.work_wait import WorkWaitRepository
    from qq_ai_bot.sandbox.db_models import SandboxTaskRunModel
    from qq_ai_bot.sandbox.task_repository import SandboxTaskRepository

    repo, lease, control = await _running_work(database, tmp_path)
    identity = control.current["id"]
    run_id = str(uuid4())
    request_id = "original-all-wait-run"
    now = datetime.now(UTC)
    tasks = SandboxTaskRepository(database)
    async with database.immediate_session() as writer:
        writer.add(
            SandboxTaskRunModel(
                request_id=request_id,
                source_conversation_id=lease.conversation_id,
                source_json=json.dumps(
                    {
                        "work_id": identity,
                        "conversation_id": lease.conversation_id,
                        "generation": lease.generation,
                    }
                ),
                payload_hash="0" * 64,
                run_id=run_id,
                status="waiting",
                created_at=now,
                updated_at=now,
            )
        )

    async def resolve(key):
        task = await tasks.by_run(key)
        if task is None or json.loads(task.source_json).get("work_id") != identity:
            return None
        return {"run_id": key, "pending": task.status != "completed"}

    control.resolve_child = resolve
    result = json.loads(
        await control.execute(
            "task_control",
            {
                "action": "wait",
                "wait_mode": "all",
                "conditions": [
                    {"kind": "owned_run", "run_id": run_id},
                    {"kind": "time_due", "after_seconds": 3600},
                ],
            },
            "wait-original-and-timer",
        )
    )
    assert result["ok"], result
    await control.settle(pending_inputs=False)
    completion = {"run_id": run_id, "status": "succeeded", "pending": False}
    await tasks.receive({"request_id": request_id, "run_id": run_id, "result": completion})
    await repo.route_child_completion(request_id)
    await repo.route_child_completion(request_id)
    waits = WorkWaitRepository(repo)
    binding = await waits.describe(identity)
    assert await waits.deliver_due(now=binding["registered_at"] + 1) == 0
    assert (await repo.get(identity))["state"] == "waiting_external"
    partial = await waits.describe(identity)
    assert partial["status"] == "active"
    assert partial["conditions"][0]["matched"]["status"] == "succeeded"
    assert partial["conditions"][1]["matched"] is None
    assert (await tasks.get(request_id)).completion_json == json.dumps(completion, sort_keys=True)
    assert await waits.deliver_due(now=partial["conditions"][1]["due"]) == 1
    assert (await repo.get(identity))["state"] == "queued"
    assert (await waits.describe(identity))["status"] == "delivered"
    assert await waits.deliver_due(now=partial["conditions"][1]["due"] + 1) == 0
    await repo.release(lease)


async def test_paused_descendant_retains_terminal_ancestors_and_resumes_without_instruction(
    database, tmp_path
):
    from datetime import UTC, datetime

    from tests.support.background_authority import approve_background_plugin

    from qq_ai_bot.plugin_host.db_models import PluginBackgroundTurnJobModel
    from yuki_plugin_sdk.models import NotificationTarget, PublishNotificationRequest

    repo, workers, parent_lease, root, sibling = await stack(database, tmp_path)
    await workers.cancel(parent_lease, root["id"], sibling)
    branch = await repo.derive(parent_lease, root["id"], "branch", "continue a real subgoal")
    leaf_id = await workers.start(
        parent_lease, branch["id"], "leaf", {"goal": "unfinished evidence"}
    )
    leaf_lease = await workers.acquire(leaf_id)
    await repo.checkpoint(
        leaf_lease, leaf_id, {"retained": "original checkpoint"}, models=2, tools=3
    )
    leaf = await repo.get(leaf_id)
    await repo.transition(leaf_lease, leaf_id, leaf["revision"], "waiting_user")
    await repo.release(leaf_lease)
    for item in (branch, root):
        await repo.checkpoint(parent_lease, item["id"], {"detail": "retained ancestor fact" * 3000})
        await repo.accept_control(
            parent_lease, item["id"], {"action": "complete", "call_key": item["id"]}
        )
        current = await repo.get(item["id"])
        await repo.transition(parent_lease, item["id"], current["revision"], "completed")
    async with database.immediate_session() as writer:
        await writer.execute(
            update(work)
            .where(work.c.id.in_((root["id"], branch["id"])))
            .values(updated=time.time() - 8 * 86400)
        )
    archived_root = await repo.accept(
        parent_lease, source_key="expired-family", source={}, goal="keep its stable child receipt"
    )
    archived_child = await workers.start(
        parent_lease, archived_root["id"], "expired-child", {"goal": "checked original result"}
    )
    archived_lease = await workers.acquire(archived_child)
    await repo.checkpoint(archived_lease, archived_child, {"checked": True}, models=1, tools=1)
    await repo.accept_control(
        archived_lease,
        archived_child,
        {"action": "complete", "call_key": "checked-done", "result": "checked original result"},
    )
    archived = await repo.get(archived_child)
    await repo.transition(archived_lease, archived_child, archived["revision"], "completed")
    await workers.finish(archived_lease)
    await repo.release(archived_lease)
    await repo.checkpoint(
        parent_lease, archived_root["id"], {"detail": "expired ancestor fact" * 3000}
    )
    await repo.accept_control(
        parent_lease, archived_root["id"], {"action": "complete", "call_key": "expired-done"}
    )
    archived = await repo.get(archived_root["id"])
    await repo.transition(parent_lease, archived["id"], archived["revision"], "completed")
    async with database.immediate_session() as writer:
        await writer.execute(
            update(work)
            .where(work.c.id.in_((archived["id"], archived_child)))
            .values(updated=time.time() - 8 * 86400)
        )
    before = await repo.get(leaf_id)
    await workers.maintain()
    assert (await workers.related(archived_root["id"], archived_child))["archived_at"] is not None
    plugin_id = "test.legacy-background-gc"
    notifications = await approve_background_plugin(
        database,
        plugin_id=plugin_id,
        bot_user_id="80001",
        group_id="20001",
        creator_user_id="10001",
    )
    legacy = {}
    for index in range(256):
        receipt = await notifications.publish(
            plugin_id=plugin_id,
            request=PublishNotificationRequest(
                event_key=f"legacy:{index}",
                event_type="fixture",
                external_source="fixture",
                target=NotificationTarget(target_type="group", target_id="20001"),
                occurred_at=datetime.now(UTC),
                summary="original background event",
                ask_agent=True,
            ),
        )
        accepted = await repo.accept(
            parent_lease,
            source_key=f"legacy-background:{receipt.source_event_id}",
            source={
                "owner": "plugin_background",
                "plugin_id": plugin_id,
                "trigger_event_id": receipt.source_event_id,
                "conversation_id": root["conversation_id"],
                "generation": root["generation"],
                "instruction": "original instruction",
                "context_data": {"original": "background context"},
            },
            goal="finished background goal",
        )
        legacy[receipt.source_event_id] = accepted["id"]
    async with database.immediate_session() as writer:
        for job in await writer.scalars(select(PluginBackgroundTurnJobModel)):
            job.work_id = legacy[job.source_event_id]
            job.status = "completed"
        await writer.execute(
            update(work)
            .where(work.c.id.in_(legacy.values()))
            .values(
                state="completed",
                checkpoint_json=json.dumps({"detail": "original background checkpoint"}),
                updated=time.time() - 7 * 86400,
            )
        )
    async with database.sessions() as reader:
        jobs_before = list(
            (await reader.execute(select(PluginBackgroundTurnJobModel.__table__))).mappings()
        )
    plain = await repo.accept(
        parent_lease, source_key="expired-plain", source={}, goal="finished unreferenced goal"
    )
    await repo.transition(parent_lease, plain["id"], plain["revision"], "completed")
    async with database.immediate_session() as writer:
        await writer.execute(
            update(work).where(work.c.id == plain["id"]).values(updated=time.time() - 9 * 86400)
        )
    for index in range(130):
        newer = await repo.accept(
            parent_lease, source_key=f"newer-terminal:{index}", source={}, goal="finished receipt"
        )
        await repo.accept_control(
            parent_lease, newer["id"], {"action": "complete", "call_key": f"newer-done:{index}"}
        )
        await repo.transition(parent_lease, newer["id"], newer["revision"], "completed")
    await repo.reclaim_terminal()
    await repo.reclaim_terminal()
    assert await repo.get(plain["id"]) is None
    async with database.sessions() as reader:
        assert (
            list((await reader.execute(select(PluginBackgroundTurnJobModel.__table__))).mappings())
            == jobs_before
        )
        archived_legacy = (
            (await reader.execute(select(work).where(work.c.id.in_(legacy.values()))))
            .mappings()
            .all()
        )
    assert {item["id"] for item in archived_legacy} == set(legacy.values())
    for item in archived_legacy:
        assert json.loads(item["checkpoint_json"]) == {"archived": True}
        source = json.loads(item["source_json"])
        assert source["owner"] == "plugin_background" and source["plugin_id"] == plugin_id
        assert legacy[source["trigger_event_id"]] == item["id"]
        assert "instruction" not in source and "context_data" not in source
    retained = await workers.related(branch["id"], leaf_id)
    assert retained["archived_at"] is None
    assert (await repo.get(leaf_id))["checkpoint_json"] == before["checkpoint_json"]
    assert (await repo.get(leaf_id))["source_json"] == before["source_json"]
    for item in (branch, root, archived_root):
        assert json.loads((await repo.get(item["id"]))["checkpoint_json"]) == {"archived": True}
    from qq_ai_bot.runtime.work_tree import ancestor_work_ids, budget_root_id

    async with database.sessions() as reader:
        assert await ancestor_work_ids(reader, leaf_id) == [branch["id"], root["id"]]
        assert await budget_root_id(reader, leaf_id) == root["id"]
        assert await budget_root_id(reader, archived_child) == archived_root["id"]
        usage = dict(
            (await reader.execute(select(budgets).where(budgets.c.root_id == archived_root["id"])))
            .mappings()
            .one()
        )
        assert usage["models"] == 1 and usage["tools"] == 1
    neutral = SimpleNamespace(repository=repo, lease=parent_lease, current=None, source={})
    resumed = await execute_subagent(
        neutral, "subagent_control", {"action": "resume", "child_id": leaf_id}, "continue-original"
    )
    assert resumed["children"][0]["state"] == "queued"
    assert (await repo.get(root["id"]))["state"] == "completed"
    assert (await repo.get(branch["id"]))["state"] == "completed"
    active = await workers.acquire(leaf_id)
    assert active is not None
    assert (await repo.get(leaf_id))["model_requests"] == 2
    assert (await repo.get(leaf_id))["tool_calls"] == 3
    await repo.release(active)
    await repo.release(parent_lease)


async def test_completed_nested_artifact_remains_readable_until_retained_tree_settles(
    database, tmp_path
):
    from datetime import UTC, datetime, timedelta

    from qq_ai_bot.persistence.models import ToolArtifactModel
    from qq_ai_bot.tool_results.artifacts import ToolArtifactRepository

    repo, workers, lease, root, _sibling = await stack(database, tmp_path)
    branch_id = (await repo.derive(lease, root["id"], "artifact-branch", "retain child evidence"))[
        "id"
    ]
    leaf_id = await workers.start(lease, branch_id, "artifact-leaf", {"goal": "check the evidence"})
    leaf_lease = await workers.acquire(leaf_id)
    store = ToolArtifactRepository(
        database, tmp_path / "artifacts", retention_seconds=60, max_artifact_bytes=100000
    )
    handle = await store.write_artifact(
        provider_id="test",
        tool_name="get_recent_chat_history",
        content="Original complete nested result.",
        media_type="text/plain",
        work_id=leaf_id,
        effect_key="original-child-read",
    )
    unrelated = await store.write_artifact(
        provider_id="test",
        tool_name="get_recent_chat_history",
        content="Separate expired result.",
        media_type="text/plain",
    )
    await repo.accept_control(
        leaf_lease,
        leaf_id,
        {"action": "complete", "call_key": "leaf-done", "result": f"Checked artifact {handle}."},
    )
    leaf = await repo.get(leaf_id)
    await repo.transition(leaf_lease, leaf_id, leaf["revision"], "completed")
    await workers.finish(leaf_lease)
    await repo.release(leaf_lease)
    await repo.accept_control(
        lease,
        branch_id,
        {"action": "complete", "call_key": "branch-done", "result": f"Retained evidence {handle}."},
    )
    branch = await repo.get(branch_id)
    await repo.transition(lease, branch_id, branch["revision"], "completed")
    async with database.immediate_session() as writer:
        await writer.execute(
            update(work)
            .where(work.c.id.in_((branch_id, leaf_id)))
            .values(updated=time.time() - 8 * 86400)
        )
        for identity in (handle, unrelated):
            saved = await writer.get(ToolArtifactModel, identity)
            saved.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    assert (await repo.get(root["id"]))["state"] == "running"
    assert handle in (await workers.related(branch_id, leaf_id))["result_json"]
    retained = await store.read(handle)
    assert retained is not None and retained["content"] == "Original complete nested result."
    assert await store.cleanup() == 1
    assert await store.read(unrelated) is None
    assert await store.read(handle) == retained
    await repo.accept_control(
        lease, root["id"], {"action": "complete", "call_key": "root-done", "result": "checked"}
    )
    root = await repo.get(root["id"])
    await repo.transition(lease, root["id"], root["revision"], "completed")
    async with database.immediate_session() as writer:
        await writer.execute(
            update(work).where(work.c.id == root["id"]).values(updated=time.time() - 8 * 86400)
        )
    assert await store.cleanup() == 1
    assert await store.read(handle) is None
    await repo.release(lease)


async def test_worker_need_input_without_reason_records_wait_without_fabricated_question(
    database, tmp_path
):
    repo, workers, parent_lease, root, identity = await stack(database, tmp_path)
    lease = await workers.acquire(identity)
    row = await repo.get(identity)

    async def validate():
        assert await repo.valid(lease)

    control = WorkControl(
        repo, lease, row["source_key"], json.loads(row["source_json"]), validate, current=row
    )
    result = json.loads(
        await control.execute("task_control", {"action": "need_input"}, "wait-for-parent")
    )
    assert result["ok"], result
    await control.settle(pending_inputs=False)
    await workers.finish(lease)
    assert (await repo.get(identity))["state"] == "waiting_user"
    pending = await repo.pending(parent_lease, work_id=root["id"])
    assert len(pending) == 1
    fact = json.loads(json.loads(pending[0]["payload_json"])["text"])
    assert fact["child_id"] == identity and fact["state"] == "waiting_user"
    assert fact["text"] == ""
    await repo.release(lease)
    await repo.release(parent_lease)


async def test_original_sandbox_runs_cancel_for_stopped_work_and_preserve_late_receipts(
    database, tmp_path
):
    from datetime import UTC, datetime
    from unittest.mock import AsyncMock
    from uuid import uuid4

    from tests.conftest import build_harness, make_settings
    from tests.support.runtime_execution import make_child_executor

    from qq_ai_bot.sandbox.db_models import SandboxTaskRunModel
    from qq_ai_bot.sandbox.task_repository import SandboxTaskRepository

    repo, workers, lease, root, failed_worker = await stack(database, tmp_path)
    branch = await repo.derive(lease, root["id"], "branch", "ordinary child")
    other_root = await repo.accept(lease, source_key="other-root", source={}, goal="ordinary root")
    live_worker = await workers.start(lease, root["id"], "live-sibling", {"goal": "continue"})
    now = datetime.now(UTC)
    runs = {identity: str(uuid4()) for identity in (failed_worker, branch["id"], other_root["id"])}
    requests = {identity: f"original:{identity}" for identity in runs}
    unknown_runs = {f"unknown:{index}": str(uuid4()) for index in range(8)}
    async with database.immediate_session() as session:
        for identity, request_id, run_id, status in (
            *(
                (branch["id"], request, run_id, "waiting")
                for request, run_id in unknown_runs.items()
            ),
            *(
                (identity, requests[identity], run_id, "waiting")
                for identity, run_id in runs.items()
            ),
            (root["id"], f"separate:{uuid4()}", str(uuid4()), "waiting"),
            (live_worker, f"separate:{uuid4()}", str(uuid4()), "waiting"),
            (failed_worker, f"separate:{uuid4()}", str(uuid4()), "completed"),
            (branch["id"], f"separate:{uuid4()}", None, "waiting"),
        ):
            session.add(
                SandboxTaskRunModel(
                    request_id=request_id,
                    source_conversation_id=root["conversation_id"],
                    source_json=json.dumps({"work_id": identity}),
                    payload_hash="original-payload",
                    run_id=run_id,
                    status=status,
                    completion_json='{"original":"completed"}' if status == "completed" else None,
                    created_at=now,
                    updated_at=now,
                )
            )
    worker_lease = await workers.acquire(failed_worker)

    async def validate():
        assert await repo.valid(lease)

    control = WorkControl(repo, lease, root["source_key"], {}, validate, current=root)
    for action, identity in (
        ("fail", failed_worker),
        ("cancel", branch["id"]),
        ("cancel", other_root["id"]),
    ):
        result = json.loads(
            await control.execute("task_control", {"action": action, "work_id": identity}, identity)
        )
        assert result["ok"], result
    assert not await repo.valid(worker_lease)
    assert (await repo.get(root["id"]))["state"] == "running"
    assert (await repo.get(live_worker))["state"] == "queued"
    harness = build_harness(database, make_settings(database.url, runtime_work_enabled=True))
    chat = harness.processor._chat
    client = AsyncMock()
    client.execute.return_value = {"error": "sandbox_unavailable", "retryable": False}
    executor = make_child_executor(
        repo,
        chat=chat,
        config=chat._runtime_config,
        runner=chat.runtime.runner,
        load_tools=AsyncMock(),
        sandbox_client=client,
    )
    async with database.sessions() as session:
        before = list((await session.execute(select(SandboxTaskRunModel.__table__))).mappings())
    await executor.cancel_commands()
    await executor.cancel_commands()
    assert {
        (call.args[0], call.args[1]["run_id"], call.kwargs["request_id"])
        for call in client.execute.await_args_list
    } == {
        ("cancel_code_run", run_id, f"worker-cancel:{run_id}")
        for run_id in (*unknown_runs.values(), *runs.values())
    }
    async with database.sessions() as session:
        after = list((await session.execute(select(SandboxTaskRunModel.__table__))).mappings())
    assert after == before
    result = {"run_id": runs[failed_worker], "status": "succeeded", "pending": False}
    tasks = SandboxTaskRepository(database)
    await tasks.receive(
        {"request_id": requests[failed_worker], "run_id": runs[failed_worker], "result": result}
    )
    receipt = await tasks.get(requests[failed_worker])
    assert receipt.run_id == runs[failed_worker] and json.loads(receipt.completion_json) == result
    assert (await repo.get(failed_worker))["state"] == "failed"
    assert (await repo.get(failed_worker))["model_requests"] == 0
    await repo.release(lease)


@pytest.mark.parametrize("cancel_kind", ["operator_cancelled", "generation_obsolete"])
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
    if cancel_kind == "operator_cancelled":
        await workers.cancel(parent_lease, parent["id"], identity)
        assert not await repo.valid(lease)
    else:
        await repo.release(lease)
        from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel

        async with database.immediate_session() as session:
            await session.execute(
                update(CanonicalConversationModel)
                .where(CanonicalConversationModel.id == parent["conversation_id"])
                .values(generation=parent["generation"] + 1)
            )
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
