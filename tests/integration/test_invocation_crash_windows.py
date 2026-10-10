"""Real SQLite admission/settlement; external dispatch has an independent log."""

import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert
from tests.conftest import MemorySender
from tests.support.work_session import WorkSession, invoke_tool
from tests.unit.test_history_dispatch_ownership import _scene, _tool
from tests.unit.test_tool_effect_audit import active_work

from qq_ai_bot.capabilities.invocation import direct_invocations
from qq_ai_bot.domain.messages import ChatMessage, ToolCall, ToolFunction
from qq_ai_bot.llm.fake import FakeLLMProvider
from qq_ai_bot.runtime.work_budget import WorkBudgetExceeded
from qq_ai_bot.runtime.work_budget_schema import budgets
from qq_ai_bot.runtime.work_control import WorkControl
from qq_ai_bot.runtime.work_repository import WorkConflict, WorkRepository
from qq_ai_bot.runtime.work_schema_v1 import effects, journal, work
from qq_ai_bot.services.turn_transcript import TurnTranscript


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


async def test_t3_preserves_host_metadata_and_rejects_conflicting_results(
    database, tmp_path, monkeypatch
):
    provider = FakeLLMProvider(
        lambda _: _tool("task_control", {"action": "accept", "goal": "send once"}, "accept")
    )
    env, harness, _chat, _state, message = await _scene(
        database, tmp_path, provider, request_limit=1
    )
    await harness.processor.handle(message, MemorySender())
    repository = WorkRepository(database)
    async with database.sessions() as reader:
        original = dict((await reader.execute(select(work))).mappings().one())
        contract = await reader.scalar(select(journal.c.contract))
    lease = await repository.acquire(original["conversation_id"], original["generation"])
    assert lease is not None

    async def validate():
        assert await repository.valid(lease)

    control = WorkControl(
        repository, lease, original["source_key"], json.loads(original["source_json"]), validate
    )
    control.current = original
    owner = WorkSession(control, contract)
    control.session = owner
    await owner.restore(TurnTranscript((ChatMessage("user", "retained send"),)))
    call = ToolCall("original", ToolFunction("send_message", '{"text":"hello"}'))
    invocation = direct_invocations(
        (call,), SimpleNamespace(work_control=control), manifest_revision=contract
    )[0]
    key = invocation.identity.operation_id
    original_writer = database.immediate_session
    metadata = []
    armed = False
    dispatched = []

    @asynccontextmanager
    async def interleaved():
        if armed and len(metadata) < 5:
            # Idempotent original-domain binding still commits a new receipt revision.
            async with original_writer() as race:
                await repository.bind_domain_receipt(
                    race, original["id"], key, metadata[0]["original_domain_ref"]
                )
                saved = await race.scalar(
                    select(effects.c.receipt_json).where(effects.c.effect_key == key)
                )
                metadata.append(json.loads(saved)["invocation"])
        async with original_writer() as writer:
            yield writer

    monkeypatch.setattr(database, "immediate_session", interleaved)

    async def external():
        nonlocal armed
        dispatched.append(key)
        sent = await env.service.execute(
            "send_message",
            {"target": {"kind": "space", "target_id": env.space}, "text": "hello"},
            replace(env.context, call_id=key),
        )
        assert sent["status"] == "succeeded"
        stored, total, root = await facts(database, key)
        assert total == root == 1
        metadata.append(stored["invocation"])
        armed = True
        return json.dumps({"ok": True, "data": sent})

    payload = await invoke_tool(owner, call, external, invocation=invocation)
    receipt, total, root = await facts(database, key)
    result = dict(receipt)
    await repository.record_effect(key, "accepted", result)
    await repository.record_effect(key, "unknown", {"error": "secondary_failure"})
    retained, _, _ = await facts(database, key)
    assert retained == receipt
    assert await invoke_tool(owner, call, external, invocation=invocation) == payload
    assert len(provider.requests) == 1 and dispatched == [key]
    assert len([entry for entry in env.bot.calls if entry[0] == "send_group_msg"]) == 1
    current = await repository.get(original["id"])
    assert current["model_requests"] == original["model_requests"] == 1
    assert current["source_json"] == original["source_json"]
    assert current["state"] == original["state"] == "queued"
    assert len(metadata) == 5
    assert receipt["invocation"]["operation_id"] == key
    assert {k: v for k, v in receipt["invocation"].items() if k != "revision"} == {
        k: v for k, v in metadata[0].items() if k != "revision"
    }
    assert receipt["result"] == payload and total == root == current["tool_calls"] == 1
    async with database.sessions() as reader:
        budget = (
            (await reader.execute(select(budgets).where(budgets.c.root_id == original["id"])))
            .mappings()
            .one()
        )
    assert budget["models"] == budget["tools"] == 1
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
        await invoke_tool(owner, changed, external)
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
        await invoke_tool(owner, call, external)
    result = await invoke_tool(owner, call, external)
    assert json.loads(result)["uncertain"] is True
    assert external_log == ["sent-before-interruption"]
    receipt, total, root = await facts(database, owner.call_key(call.id))
    assert receipt["invocation"]["dispatch_started"] is True
    assert total == root == 1


async def test_late_receipt_settles_original_after_cancel_without_new_admission(database, tmp_path):
    owner, invocation = await prepared(database, tmp_path)
    repository = owner.control.repository
    lease, identity = owner.control.lease, owner.control.current["id"]
    key = invocation.identity.operation_id
    assert await repository.admit_dispatch(lease, identity, key)
    waiting = direct_invocations(
        (ToolCall("queued", ToolFunction("send_message", '{"text":"later"}')),),
        SimpleNamespace(work_control=owner.control),
        manifest_revision="contract",
    )[0]
    assert await repository.prepare_effect(
        lease,
        identity,
        waiting.identity.operation_id,
        "tool",
        invocation=waiting.durable_metadata(),
        outcome={"tool": "send_message", "side_effecting": True},
    )
    await repository.cancel(lease.conversation_id)
    # The already-dispatched send reports back after the hard boundary.
    late = {"result": '{"ok":true}', "outcome": {"ok": True, "side_effecting": True}}
    await repository.record_effect(key, "accepted", late)
    receipt, total, root = await facts(database, key)
    assert receipt["result"] == late["result"] and total == root == 1
    # The narrow settlement path cannot authorize another dispatch.
    other = direct_invocations(
        (ToolCall("after-cancel", ToolFunction("send_message", "{}")),),
        SimpleNamespace(work_control=owner.control),
        manifest_revision="contract",
    )[0]
    with pytest.raises(WorkConflict):
        await repository.prepare_effect(
            lease,
            identity,
            other.identity.operation_id,
            "tool",
            invocation=other.durable_metadata(),
        )
    # T1-registered but undispatched work is closed by the boundary, not charged.
    with pytest.raises(WorkConflict, match="work_activation_obsolete"):
        await repository.admit_dispatch(lease, identity, waiting.identity.operation_id)
    # The original reports "already admitted" and is never re-dispatched or recharged.
    assert not await repository.admit_dispatch(lease, identity, key)
    queued, total, root = await facts(database, waiting.identity.operation_id)
    assert queued["invocation"]["dispatch_started"] is False and total == root == 1
