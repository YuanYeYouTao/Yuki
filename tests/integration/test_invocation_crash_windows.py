"""Real SQLite admission/settlement; external dispatch has an independent log."""

import asyncio
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert
from tests.unit.test_tool_effect_audit import active_work

from qq_ai_bot.capabilities.invocation import direct_invocations
from qq_ai_bot.domain.messages import ToolCall, ToolFunction
from qq_ai_bot.runtime.work_budget import WorkBudgetExceeded
from qq_ai_bot.runtime.work_budget_schema import budgets
from qq_ai_bot.runtime.work_repository import WorkConflict
from qq_ai_bot.runtime.work_schema_v1 import effects, work


async def prepared(database, tmp_path):
    _env, owner, _runtime = await active_work(database, tmp_path)
    call = ToolCall("original", ToolFunction("send_message", '{"text":"hello"}'))
    invocation = direct_invocations(
        (call,), SimpleNamespace(work_control=owner.control), manifest_revision="contract"
    )[0]
    repository = owner.control.repository
    assert await repository.prepare_effect(
        owner.control.lease,
        owner.control.current["id"],
        invocation.identity.operation_id,
        "tool",
        invocation=invocation.durable_metadata(),
        outcome={"tool": "send_message", "side_effecting": True},
    )
    return owner, invocation


async def facts(database, key):
    async with database.sessions() as reader:
        row = (
            (await reader.execute(select(effects).where(effects.c.effect_key == key)))
            .mappings()
            .one()
        )
        total = await reader.scalar(select(work.c.tool_calls).where(work.c.id == row["work_id"]))
        root = await reader.scalar(
            select(budgets.c.tools).where(budgets.c.root_id == row["work_id"])
        )
    return json.loads(row["receipt_json"]), total, root


async def test_t1_does_not_charge_or_dispatch_and_original_t2_charges_once(database, tmp_path):
    owner, invocation = await prepared(database, tmp_path)
    key = invocation.identity.operation_id
    receipt, total, root = await facts(database, key)
    assert receipt["invocation"]["dispatch_started"] is False
    assert total == 0 and root is None
    results = await asyncio.gather(
        *(
            owner.control.repository.admit_dispatch(
                owner.control.lease, owner.control.current["id"], key
            )
            for _ in range(2)
        )
    )
    assert sorted(results) == [False, True]
    receipt, total, root = await facts(database, key)
    assert receipt["invocation"]["dispatch_started"] is True
    assert receipt["invocation"]["budget_admitted"] is True
    assert total == root == 1


async def test_budget_rejection_rolls_back_dispatch_marker_and_all_usage(database, tmp_path):
    owner, invocation = await prepared(database, tmp_path)
    identity = owner.control.current["id"]
    async with database.sessions() as writer, writer.begin():
        await writer.execute(insert(budgets).values(root_id=identity, tool_limit=0))
    with pytest.raises(WorkBudgetExceeded):
        await owner.control.repository.admit_dispatch(
            owner.control.lease, identity, invocation.identity.operation_id
        )
    receipt, total, root = await facts(database, invocation.identity.operation_id)
    assert receipt["invocation"]["dispatch_started"] is False
    assert receipt["invocation"]["budget_admitted"] is False
    assert total == root == 0


async def test_lost_dispatch_confirmation_queries_marker_without_recharging(database, tmp_path):
    owner, invocation = await prepared(database, tmp_path)
    repository = owner.control.repository
    key = invocation.identity.operation_id
    assert await repository.admit_dispatch(owner.control.lease, owner.control.current["id"], key)
    # Process restarts after COMMIT, before the caller receives its confirmation.
    assert not await repository.admit_dispatch(
        owner.control.lease, owner.control.current["id"], key
    )
    receipt, total, root = await facts(database, key)
    assert total == root == 1
    assert receipt["invocation"]["dispatch_started"]
    assert json.loads(await owner.journal.effect_result(key))["uncertain"]


async def test_t3_preserves_host_metadata_and_rejects_conflicting_results(database, tmp_path):
    owner, invocation = await prepared(database, tmp_path)
    repository = owner.control.repository
    key = invocation.identity.operation_id
    assert await repository.admit_dispatch(owner.control.lease, owner.control.current["id"], key)
    result = {"result": '{"ok":true}', "outcome": {"ok": True, "side_effecting": True}}
    await repository.record_effect(key, "accepted", result)
    await repository.record_effect(key, "accepted", result)
    await repository.record_effect(key, "unknown", {"error": "secondary_failure"})
    receipt, total, root = await facts(database, key)
    assert receipt["invocation"]["operation_id"] == key
    assert receipt["invocation"]["revision"] == 2
    assert receipt["result"] == result["result"] and total == root == 1
    with pytest.raises(WorkConflict, match="work_effect_receipt_conflict"):
        await repository.record_effect(key, "accepted", {**result, "result": '{"ok":false}'})
    with pytest.raises(WorkConflict, match="work_effect_metadata_conflict"):
        await repository.record_effect(key, "accepted", {**result, "invocation": {"version": 1}})


async def test_changed_arguments_under_original_id_cannot_reuse_or_dispatch(database, tmp_path):
    owner, invocation = await prepared(database, tmp_path)
    changed = ToolCall(invocation.call.id, ToolFunction("send_message", '{"text":"changed"}'))
    dispatched = []

    async def external():
        dispatched.append("send")
        return '{"ok":true}'

    with pytest.raises(WorkConflict, match="invocation_content_conflict"):
        await owner.execute(changed, external, allow_pending=True)
    assert dispatched == []
    _, total, root = await facts(database, invocation.identity.operation_id)
    assert total == 0 and root is None


async def test_unknown_dispatch_is_never_replayed_after_reentry(database, tmp_path):
    _env, owner, _runtime = await active_work(database, tmp_path)
    call = ToolCall("original", ToolFunction("send_message", "{}"))
    external_log = []

    async def external():
        external_log.append("sent-before-interruption")
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await owner.execute(call, external, allow_pending=True)
    result = await owner.execute(call, external, allow_pending=True)
    assert json.loads(result)["uncertain"] is True
    assert external_log == ["sent-before-interruption"]
    receipt, total, root = await facts(database, owner.call_key(call.id))
    assert receipt["invocation"]["dispatch_started"] is True
    assert total == root == 1
