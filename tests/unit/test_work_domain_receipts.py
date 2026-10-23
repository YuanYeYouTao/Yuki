"""Original domain facts survive the Work T2/T3 gap without another dispatch."""

import json
from dataclasses import replace
from hashlib import sha256
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select, update
from tests.unit.test_external_agent_repository import prepare, register_device
from tests.unit.test_external_agent_work import case as work_case
from tests.unit.test_memory_mutation import _context, _service
from tests.unit.test_memory_operation_identity import create_request
from tests.unit.test_tool_effect_audit import active_work

from yuki.capabilities.invocation import direct_invocations
from yuki.domain.messages import ToolCall, ToolFunction
from yuki.external_agents.db_models import AgentOperationModel
from yuki.persistence.models import MemoryFactModel, MemoryMutationReceiptModel
from yuki.runtime.effect_outcomes import ResultCapture, current_result_capture, execution_finished
from yuki.runtime.work_activation import current_work_control
from yuki.runtime.work_journal import WorkJournal
from yuki.runtime.work_schema_v1 import effects, work
from yuki.sandbox.db_models import SandboxTaskRunModel

case = work_case


async def admitted(control, tool, arguments, call_id="original-call"):
    invocation = direct_invocations(
        (ToolCall(call_id, ToolFunction(tool, json.dumps(arguments))),),
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
    assert await control.repository.admit_dispatch(control.lease, control.current["id"], key)
    return key


async def read_result(database, control, key):
    capture = ResultCapture(control.current["id"], key)
    token = current_result_capture.set(capture)
    try:
        result = json.loads(await WorkJournal(control.repository).effect_result(key))
    finally:
        current_result_capture.reset(token)
    async with database.read_session() as reader:
        row = (
            (await reader.execute(select(effects).where(effects.c.effect_key == key)))
            .mappings()
            .one()
        )
        counters = (
            await reader.execute(
                select(work.c.model_requests, work.c.tool_calls).where(
                    work.c.id == control.current["id"]
                )
            )
        ).one()
    assert row["state"] == "prepared"
    assert json.loads(row["receipt_json"])["invocation"]["dispatch_started"] is True
    return result, capture.evidence, tuple(counters)


@pytest.mark.asyncio
async def test_memory_original_commit_and_current_fact_are_separate(database, tmp_path):
    _, owner, runtime = await active_work(database, tmp_path)
    control = owner.control
    mutations, _facts, ledger, _ = _service(database)
    event = await ledger.get_event(runtime.trigger_event_id)
    request = create_request("a remembered preference")
    key = await admitted(control, "memory_change", request.model_dump(mode="json"))
    token = current_result_capture.set(ResultCapture(control.current["id"], key))
    source_token = current_work_control.set(control)
    try:
        committed = await mutations.mutate(request, replace(_context(event), operation_id=key))
    finally:
        current_result_capture.reset(token)
        current_work_control.reset(source_token)
    assert committed.ok
    # A later valid mutation can supersede the original version; it cannot undo its receipt.
    from yuki.memory.mutation.models import MemoryMutationOperation, MemoryMutationRequest

    later = await mutations.mutate(
        MemoryMutationRequest(
            operation=MemoryMutationOperation.CORRECT,
            fact_id=committed.new_fact_id,
            new_content="later preference",
        ),
        replace(_context(event), operation_id="later-independent-call"),
    )
    assert later.ok
    await database.engine.dispose()
    before = await read_result(database, control, key)
    result, evidence, _ = before
    assert result["data"]["mutation_id"] == committed.mutation_id
    assert result["data"]["new_fact_id"] == committed.new_fact_id
    assert result["data"]["outcome"] == "committed"
    assert result["mutation_committed"] is True
    assert evidence["ok"] and evidence["executed"] and evidence["mutation_committed"]
    assert not evidence["pending"] and not evidence["uncertain"]
    assert await read_result(database, control, key) == before
    async with database.read_session() as reader:
        assert await reader.scalar(select(func.count()).select_from(MemoryFactModel)) == 2
        assert (
            await reader.scalar(select(func.count()).select_from(MemoryMutationReceiptModel)) == 2
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "action,response,expected_status,finished",
    [
        ("cancel", {"status": "unknown", "pending": True, "admitted": True}, "admitted", False),
        ("steer", {"status": "succeeded", "pending": False, "admitted": True}, "admitted", False),
        ("respond", {"status": "succeeded", "pending": False, "admitted": True}, "admitted", False),
        ("cancel", {"status": "cancelled", "pending": False}, "cancelled", True),
        (
            "detach",
            {"status": "detached", "pending": False, "native_pending": True},
            "detached",
            True,
        ),
        ("steer", {"status": "running", "pending": True}, "running", False),
        ("steer", {"status": "unknown", "pending": True}, "unknown", False),
        (
            "respond",
            {"status": "failed", "pending": False, "error": "denied", "executed": False},
            "failed",
            True,
        ),
    ],
)
async def test_native_control_original_result_without_run(
    case, action, response, expected_status, finished
):
    control = case.control
    native = await register_device(case.db, case.source["actor_person_id"])
    native.session = (
        await native.repository.remember_sessions(native.instance, native.actor, ["thread"])
    )["thread"]
    native.source = {
        **case.source,
        "work_id": control.current["id"],
        "conversation_id": control.lease.conversation_id,
    }
    arguments = {"action": action}
    key = await admitted(control, "external_agent", arguments)
    request_id = sha256(key.encode()).hexdigest()
    await prepare(
        native, identity=request_id, action=action, arguments=arguments, lease=control.lease
    )
    await native.repository.response(request_id, response)
    await case.db.engine.dispose()
    result, evidence, counters = await read_result(case.db, control, key)
    assert result["data"]["operation_id"] == request_id
    assert result["data"]["action"] == action
    assert evidence["status"] == expected_status
    assert execution_finished(evidence) is finished
    assert evidence["executed"] is response.get("executed", True)
    assert evidence["uncertain"] is (expected_status == "unknown")
    assert evidence["pending"] is (bool(response.get("pending")) and expected_status != "admitted")
    if expected_status == "admitted":
        assert result["ok"] and not result.get("uncertain", False)
        assert result["mutation_committed"] is True
    assert (await read_result(case.db, control, key))[2] == counters
    async with case.db.read_session() as reader:
        assert await reader.get(SandboxTaskRunModel, request_id) is None
        assert await reader.scalar(select(func.count()).select_from(AgentOperationModel)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("mismatch", ["work_id", "action", "actor_person_id", "missing"])
async def test_native_control_does_not_borrow_another_receipt(case, mismatch):
    native = await register_device(case.db, case.source["actor_person_id"])
    native.session = (
        await native.repository.remember_sessions(native.instance, native.actor, ["thread"])
    )["thread"]
    native.source = {
        **case.source,
        "work_id": case.control.current["id"],
        "conversation_id": case.control.lease.conversation_id,
    }
    key = await admitted(case.control, "external_agent", {"action": "steer"})
    request_id = sha256(key.encode()).hexdigest()
    if mismatch != "missing":
        await prepare(native, identity=request_id, lease=case.control.lease)
        await native.repository.response(
            request_id, {"status": "succeeded", "pending": False, "admitted": True}
        )
        async with case.db.immediate_session() as writer:
            if mismatch == "actor_person_id":
                source = {**case.source, "actor_person_id": "different-person"}
                await writer.execute(
                    update(work)
                    .where(work.c.id == case.control.current["id"])
                    .values(source_json=json.dumps(source))
                )
            else:
                await writer.execute(
                    update(AgentOperationModel)
                    .where(AgentOperationModel.id == request_id)
                    .values(**{mismatch: "different"})
                )
    result, evidence, _ = await read_result(case.db, case.control, key)
    assert result["error"] == "execution_outcome_unknown"
    assert evidence["uncertain"] and not evidence["ok"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome",
    ["committed_as_contested", "no_change", "rejected", "processing", "missing", "wrong_ref"],
)
async def test_memory_receipt_outcomes_and_missing_relationship(database, tmp_path, outcome):
    from datetime import UTC, datetime

    from yuki.memory.mutation.models import (
        MemoryDecisionActorType,
        MemoryMutationAppliedOperation,
        MemoryMutationOperation,
        MemoryMutationOutcome,
    )
    from yuki.memory.mutation.repository import MemoryMutationReceiptRepository
    from yuki.memory.mutation.service import _fingerprint
    from yuki.runtime.work_repository import WorkRepository

    _, owner, runtime = await active_work(database, tmp_path)
    control = owner.control
    key = await admitted(control, "memory_change", {})
    receipts = MemoryMutationReceiptRepository(database)
    async with database.immediate_session() as writer:
        if outcome != "missing":
            receipt = await receipts.reserve(
                mutation_id="domain-mutation",
                idempotency_key=_fingerprint(
                    {"operation_id": "another-call" if outcome == "wrong_ref" else key}
                ),
                claim_fingerprint="claim",
                target_fingerprint="target",
                trigger_event_id=runtime.trigger_event_id,
                conversation_key=runtime.conversation_id,
                current_group_id=None,
                turn_origin="user_message",
                delegation_mode="main",
                trigger_actor_user_id=runtime.actor_user_id,
                decision_actor_type=MemoryDecisionActorType.AGENT,
                decision_actor_id=None,
                executed_by_bot_user_id=runtime.bot_user_id,
                requested_operation=MemoryMutationOperation.CREATE,
                created_at=datetime.now(UTC),
                session=writer,
            )
            if outcome != "processing":
                await receipts.finalize(
                    receipt.id,
                    applied_operation=MemoryMutationAppliedOperation.NOOP,
                    old_fact_id=None,
                    new_fact_id=None,
                    outcome=MemoryMutationOutcome.COMMITTED
                    if outcome == "wrong_ref"
                    else MemoryMutationOutcome(outcome),
                    reason_code="original_reason",
                    session=writer,
                )
        await WorkRepository.bind_domain_receipt(
            writer, control.current["id"], key, "memory:domain-mutation"
        )
    result, evidence, _ = await read_result(database, control, key)
    known = outcome not in {"processing", "missing", "wrong_ref"}
    assert evidence["uncertain"] is not known
    if known:
        assert result["data"]["outcome"] == outcome
        assert result["mutation_committed"] is (outcome == "committed_as_contested")
        assert evidence["mutation_committed"] is (outcome == "committed_as_contested")
        assert evidence["executed"]
    else:
        assert result["error"] == "execution_outcome_unknown"
    # An accepted marker without a complete result must consume recovered evidence too.
    if known:
        async with database.immediate_session() as writer:
            await writer.execute(
                update(effects).where(effects.c.effect_key == key).values(state="accepted")
            )
        capture = ResultCapture(control.current["id"], key)
        token = current_result_capture.set(capture)
        try:
            accepted = json.loads(await WorkJournal(control.repository).effect_result(key))
        finally:
            current_result_capture.reset(token)
        assert accepted["data"] == result["data"]
        assert capture.evidence == evidence
