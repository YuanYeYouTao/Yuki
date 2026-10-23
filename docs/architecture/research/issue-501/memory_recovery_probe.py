"""Research probes for 8aef3f0b: two assertions intentionally expose unfixed defects."""

# Fixture imports below are consumed by pytest's name-based injection.
# ruff: noqa: F401, F811

import pytest
from tests.integration.test_plugin_memory_facade_contract import memory_host


@pytest.mark.asyncio
async def test_replay_correction_after_later_correction(database, memory_host):
    env, host, invocation, _facts, _ = memory_host

    async def sequence():
        with host.bind(invocation):
            add = await host.memory.add(
                scope_type="person",
                subject_id=env.person,
                content="original",
                source_type="plugin",
                confidence=0.4,
            )
            first = await host.memory.update(
                add.data["memory"]["memory_id"], content="first correction"
            )
            second = await host.memory.update(first.data["memory_id"], content="second correction")
            return first, second

    first = await sequence()
    await database.engine.dispose()
    replay = await sequence()
    print("ORIGINAL", first)
    print("REPLAY", replay)
    assert replay == first


@pytest.mark.asyncio
async def test_add_delete_replay_retains_fact_id_and_effect(database, memory_host):
    env, host, invocation, _facts, _ = memory_host

    async def sequence():
        with host.bind(invocation):
            added = await host.memory.add(
                scope_type="person",
                subject_id=env.person,
                content="original",
                source_type="plugin",
                confidence=0.4,
            )
            deleted = await host.memory.delete(added.data["memory"]["memory_id"])
            return added, deleted

    first = await sequence()
    await database.engine.dispose()
    replay = await sequence()
    print("ADD_DELETE_ORIGINAL", first)
    print("ADD_DELETE_REPLAY", replay)
    assert replay[0].ok and replay[1].ok
    assert replay[0].data["memory"]["memory_id"] == first[0].data["memory"]["memory_id"]
    assert replay[1] == first[1]


@pytest.mark.asyncio
async def test_memory_domain_commit_without_t3_is_known_from_receipt(database, tmp_path):
    import json
    from dataclasses import replace
    from types import SimpleNamespace

    from sqlalchemy import func, select
    from tests.unit.test_memory_mutation import _context, _service
    from tests.unit.test_memory_operation_identity import create_request
    from tests.unit.test_tool_effect_audit import active_work

    from yuki.capabilities.invocation import direct_invocations
    from yuki.domain.messages import ToolCall, ToolFunction
    from yuki.persistence.models import MemoryFactModel, MemoryMutationReceiptModel
    from yuki.runtime.effect_outcomes import ResultCapture, current_result_capture
    from yuki.runtime.work_activation import current_work_control
    from yuki.runtime.work_schema_v1 import effects

    _, owner, runtime = await active_work(database, tmp_path)
    mutations, _facts, ledger, _ = _service(database)
    event = await ledger.get_event(runtime.trigger_event_id)
    request = create_request("a remembered preference")
    call = ToolCall("original-memory", ToolFunction("memory_change", request.model_dump_json()))
    invocation = direct_invocations(
        (call,), SimpleNamespace(work_control=owner.control), manifest_revision="contract"
    )[0]
    key = invocation.identity.operation_id
    repository = owner.control.repository
    work_id = owner.control.current["id"]
    assert await repository.prepare_effect(
        owner.control.lease,
        work_id,
        key,
        "tool",
        invocation=invocation.durable_metadata(),
        outcome={"tool": "memory_change", "side_effecting": True},
    )
    assert await repository.admit_dispatch(owner.control.lease, work_id, key)
    capture_token = current_result_capture.set(ResultCapture(work_id, key))
    control_token = current_work_control.set(owner.control)
    try:
        result = await mutations.mutate(request, replace(_context(event), operation_id=key))
    finally:
        current_work_control.reset(control_token)
        current_result_capture.reset(capture_token)
    assert result.ok
    # Reconstruct the persisted boundary: domain commit finished, T3 not executed.
    await database.engine.dispose()
    async with database.read_session() as reader:
        row = (
            (await reader.execute(select(effects).where(effects.c.effect_key == key)))
            .mappings()
            .one()
        )
        stored = json.loads(row["receipt_json"])
        receipt = await reader.scalar(
            select(MemoryMutationReceiptModel).where(
                MemoryMutationReceiptModel.mutation_id == result.mutation_id
            )
        )
        print("WORK_BOUNDARY", row["state"], stored)
        print("MEMORY_RECEIPT", receipt.outcome, receipt.mutation_id, receipt.new_fact_id)
        assert await reader.scalar(select(func.count()).select_from(MemoryFactModel)) == 1
        assert stored["invocation"]["original_domain_ref"] == "memory:" + receipt.mutation_id
        assert receipt.outcome == "committed"
    recovered = json.loads(await owner.journal.effect_result(key, original_tool="memory_change"))
    print("RECOVERED_MEMORY", recovered)
    assert recovered["ok"] is True and not recovered.get("uncertain", False)
