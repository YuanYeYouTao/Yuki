"""Paid recovery precedes newly queued inputs in the actual Runner."""

import json
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

# P10: fixed typed backend fixture, original assertions retained.
from tests.support.agent_backend import StubAgentBackend
from tests.support.work_compaction import summary_json
from tests.unit.test_work_compaction_capacity import (
    _grow,
    _runtime,
    _seed_runner_contract,
    _session,
    _steer,
)

from qq_ai_bot.domain.messages import (
    ChatResponse,
    ChatTool,
    ProviderContinuation,
    ToolCall,
    ToolFunction,
)
from qq_ai_bot.llm.fake import FakeLLMProvider
from qq_ai_bot.model_runtime.models import ModelExecutionPriority
from qq_ai_bot.runtime.activation_outcome import SegmentBudgetReached
from qq_ai_bot.runtime.work_control import WorkControl
from qq_ai_bot.runtime.work_repository import WorkCapacityError, WorkConflict
from qq_ai_bot.runtime.work_schema_v1 import effects, inputs, journal
from qq_ai_bot.runtime.work_session import WorkSession


async def test_runner_finishes_paid_candidate_before_pending_steer(database, tmp_path, monkeypatch):
    control, session, initial = await _session(database, tmp_path)
    _grow(session.transcript)
    provider = FakeLLMProvider(
        lambda request: (
            summary_json(request.messages[-1].content)
            if request.structured_output
            else "Continue the original task."
        )
    )
    runner, runtime = await _runtime(
        database, control, initial, provider, contract_workspace=tmp_path / "main-contract"
    )
    runtime, main = await _seed_runner_contract(runner, runtime, session, initial)
    save = session.save

    async def fail_candidate(phase, *args, **kwargs):
        if phase == "paired" and session.compaction_ready_summary is None:
            raise WorkConflict("candidate_publish_failed")
        await save(phase, *args, **kwargs)

    monkeypatch.setattr(session, "save", fail_candidate)
    with pytest.raises(WorkConflict, match="candidate_publish_failed"):
        await runner._compact_work(runtime, ModelExecutionPriority.FOREGROUND, 128000, main)
    assert session.compaction_ready_summary is not None
    assert len(provider.requests) == 1
    pending = await control.repository.enqueue(
        control.lease.conversation_id,
        control.lease.generation,
        "paid-stage-new-steer",
        kind="message",
        event_id=1,
        work_id=control.current["id"],
        ready=False,
    )
    text = "New requirement after the already paid candidate."
    assert await control.repository.prepare_input(pending, {"text": text})
    await control.repository.release(control.lease)
    lease = await control.repository.acquire(
        control.lease.conversation_id, control.lease.generation
    )
    resumed = WorkControl(
        control.repository, lease, control.source_key, control.source, AsyncMock()
    )
    resumed.current = await control.repository.get(control.current["id"])
    before = resumed.current["model_requests"]
    await runner.run(initial, replace(runtime, work_control=resumed), None)
    paid = [request for request in provider.requests if request.structured_output]
    primary = [request for request in provider.requests if not request.structured_output]
    assert len(paid) == 1
    assert len(primary) == 1
    assert sum((message.content or "").count(text) for message in primary[0].messages) == 1
    assert "compaction_staging" not in resumed.session.progress
    current = await control.repository.get(control.current["id"])
    assert current["model_requests"] == before + 1
    async with database.sessions() as reader:
        item = (await reader.execute(select(inputs).where(inputs.c.id == pending))).mappings().one()
        assert item["state"] == "consumed"
    await control.repository.release(lease)


async def test_tool_pair_and_paid_retirement_share_writer_and_recover_original_effect(
    database, tmp_path, monkeypatch
):
    control, session, initial = await _session(database, tmp_path)
    _grow(session.transcript)
    provider = FakeLLMProvider(
        lambda request: (
            summary_json(request.messages[-1].content, "Verified paid finding survives the fault.")
            if request.structured_output
            else ChatResponse(
                "A real complete response.",
                0,
                tool_calls=(ToolCall("original-probe", ToolFunction("read_probe", "{}")),),
            )
        )
    )
    runner, runtime = await _runtime(database, control, initial, provider)
    runtime = replace(runtime, fixed_tools=(ChatTool("read_probe", "Read", {"type": "object"}),))
    session.contract = runner.work_contract(runtime.runtime_config, initial, runtime.fixed_tools)
    await session.save("paired")
    from qq_ai_bot.domain.messages import ChatRequest

    main = ChatRequest(messages=session.transcript.request().messages, tools=runtime.fixed_tools)
    save = session.save

    async def fail_candidate(phase, *args, **kwargs):
        if phase == "paired" and session.compaction_ready_summary is None:
            raise WorkConflict("candidate_publish_failed")
        await save(phase, *args, **kwargs)

    monkeypatch.setattr(session, "save", fail_candidate)
    with pytest.raises(WorkConflict):
        await runner._compact_work(runtime, ModelExecutionPriority.FOREGROUND, 128000, main)
    await control.repository.release(control.lease)
    lease = await control.repository.acquire(control.lease.conversation_id, 1)
    resumed = WorkControl(
        control.repository, lease, control.source_key, control.source, AsyncMock()
    )
    resumed.current = await control.repository.get(control.current["id"])
    failed = AsyncMock(side_effect=WorkCapacityError("work_compaction_no_capacity_improvement"))
    monkeypatch.setattr(runner, "_compact_work", failed)
    original_save = WorkSession.save
    paired_attempts = []

    async def fail_retirement(self, phase, *args, **kwargs):
        if phase == "paired":
            paired_attempts.append((bool(self.progress.get("compaction_staging")), kwargs))
            raise WorkConflict("paired_publication_failed")
        await original_save(self, phase, *args, **kwargs)

    monkeypatch.setattr(WorkSession, "save", fail_retirement)
    execute = AsyncMock(return_value='{"ok":true,"executed":true,"data":"Original receipt"}')
    backend = StubAgentBackend(
        execute_call=execute,
        is_side_effecting=lambda *_: False,
        parallel_safe=lambda *_: False,
        finalize=lambda content, _: content,
    )
    await runner.run(initial, replace(runtime, work_control=resumed), backend)
    execute.assert_awaited_once()
    assert len(paired_attempts) == 1 and paired_attempts[0][0] is False
    assert "communication_updates" in paired_attempts[0][1]
    async with database.sessions() as reader:
        saved = (await reader.execute(select(journal))).mappings().one()
        assert saved["phase"] == "response"
        original_effect = dict((await reader.execute(select(effects))).mappings().one())
        assert original_effect["state"] == "accepted"
    pending = await control.repository.enqueue(
        control.lease.conversation_id,
        1,
        "paid-retirement-fault-pending",
        kind="message",
        event_id=1,
        work_id=control.current["id"],
        ready=False,
    )
    text = "Resume the original effect without replaying the probe."
    assert await control.repository.prepare_input(pending, {"text": text})
    monkeypatch.setattr(WorkSession, "save", original_save)
    await control.repository.release(lease)
    lease = await control.repository.acquire(control.lease.conversation_id, 1)
    resumed = WorkControl(
        control.repository, lease, control.source_key, control.source, AsyncMock()
    )
    resumed.current = await control.repository.get(control.current["id"])
    provider._responder = lambda request: "A completed fresh response."
    await runner.run(initial, replace(runtime, work_control=resumed), backend)
    execute.assert_awaited_once()
    failed.assert_awaited_once()
    assert "compaction_staging" not in resumed.session.progress
    assert resumed.session.progress["task_material"]["paid_observations"]["pending"]
    assert (
        sum((message.content or "").count(text) for message in provider.requests[-1].messages) == 1
    )
    assert len([request for request in provider.requests if request.structured_output]) == 1
    async with database.sessions() as reader:
        assert dict((await reader.execute(select(effects))).mappings().one()) == original_effect
        assert (
            await reader.scalar(select(inputs.c.state).where(inputs.c.id == pending)) == "consumed"
        )
    await control.repository.release(lease)


async def test_partial_paid_pages_finish_at_budget_boundary_before_new_input(database, tmp_path):
    control, session, initial = await _session(database, tmp_path)
    original_inputs = [
        await _steer(control, session, index, f"Original requirement {index}.")
        for index in range(40)
    ]
    _grow(session.transcript)
    sources = []

    def respond(request):
        if request.structured_output:
            source = json.loads(request.messages[-1].content)
            sources.append(source)
            return summary_json(source)
        return "Complete response after all paid pages."

    provider = FakeLLMProvider(respond)
    runner, runtime = await _runtime(
        database, control, initial, provider, contract_workspace=tmp_path / "main-contract"
    )
    runtime, main = await _seed_runner_contract(runner, runtime, session, initial)
    control.segment_model_limit = 2
    with pytest.raises(SegmentBudgetReached):
        await runner._compact_work(runtime, ModelExecutionPriority.FOREGROUND, 128000, main)
    assert len(sources) == 2
    pending = await control.repository.enqueue(
        control.lease.conversation_id,
        1,
        "partial-paid-pending",
        kind="message",
        event_id=1,
        work_id=control.current["id"],
        ready=False,
    )
    text = "New input must stay pending until the primary request has budget."
    assert await control.repository.prepare_input(pending, {"text": text})
    await control.repository.release(control.lease)
    lease = await control.repository.acquire(control.lease.conversation_id, 1)
    resumed = WorkControl(
        control.repository, lease, control.source_key, control.source, AsyncMock()
    )
    resumed.current = await control.repository.get(control.current["id"])
    await runner.run(initial, replace(runtime, work_control=resumed), None)
    assert len(sources) == 3
    assert [
        item["input_id"] for source in sources for item in source["task_inputs"]
    ] == original_inputs
    assert len(provider.requests) == 3
    async with database.sessions() as reader:
        assert (
            await reader.scalar(select(inputs.c.state).where(inputs.c.id == pending)) == "pending"
        )
    assert "compaction_staging" not in resumed.session.progress
    await control.repository.release(lease)
    lease = await control.repository.acquire(control.lease.conversation_id, 1)
    resumed = WorkControl(
        control.repository, lease, control.source_key, control.source, AsyncMock()
    )
    resumed.current = await control.repository.get(control.current["id"])
    await runner.run(initial, replace(runtime, work_control=resumed), None)
    assert len(provider.requests) == 4 and len(sources) == 3
    assert (
        sum((message.content or "").count(text) for message in provider.requests[-1].messages) == 1
    )
    assert (await control.repository.get(control.current["id"]))["model_requests"] == 4
    async with database.sessions() as reader:
        assert (
            await reader.scalar(select(inputs.c.state).where(inputs.c.id == pending)) == "consumed"
        )
    await control.repository.release(lease)


@pytest.mark.parametrize("same_activation", [False, True])
@pytest.mark.parametrize("invalid_final", [False, True])
async def test_soft_failed_paid_candidate_retires_only_after_response_without_starving_input(
    database, tmp_path, monkeypatch, same_activation, invalid_final
):
    control, session, initial = await _session(database, tmp_path)
    _grow(session.transcript)
    provider = FakeLLMProvider(
        lambda request: (
            summary_json(request.messages[-1].content, "Already paid and verified finding.")
            if request.structured_output
            else "A new complete response."
        )
    )
    runner, runtime = await _runtime(
        database, control, initial, provider, contract_workspace=tmp_path / "main-contract"
    )
    runtime, main = await _seed_runner_contract(runner, runtime, session, initial)
    save = session.save

    async def fail_publication(phase, *args, **kwargs):
        if phase == "paired" and session.compaction_ready_summary is None:
            raise WorkConflict("candidate_publish_failed")
        await save(phase, *args, **kwargs)

    monkeypatch.setattr(session, "save", fail_publication)
    with pytest.raises(WorkConflict):
        await runner._compact_work(runtime, ModelExecutionPriority.FOREGROUND, 128000, main)
    if invalid_final:
        session.progress["compaction_staging"]["final_summary"] = "invalid final is not a fact"
        await save("paired")
    before_sequence = session.transcript.request()
    pending = await control.repository.enqueue(
        control.lease.conversation_id,
        1,
        "soft-paid-pending",
        kind="message",
        event_id=1,
        work_id=control.current["id"],
        ready=False,
    )
    text = "Pending must be answered once after a genuine paired response."
    assert await control.repository.prepare_input(pending, {"text": text})
    await control.repository.release(control.lease)
    lease = await control.repository.acquire(control.lease.conversation_id, 1)
    resumed = WorkControl(
        control.repository, lease, control.source_key, control.source, AsyncMock()
    )
    resumed.current = await control.repository.get(control.current["id"])
    before_budget = resumed.current["model_requests"]
    failed = AsyncMock(side_effect=WorkCapacityError("work_compaction_no_capacity_improvement"))
    monkeypatch.setattr(runner, "_compact_work", failed)
    await runner.run(
        initial,
        replace(runtime, work_control=resumed, max_model_requests=2 if same_activation else 1),
        None,
    )
    failed.assert_awaited_once()
    primary = [request for request in provider.requests if not request.structured_output]
    assert primary[0].messages == before_sequence.messages
    assert text not in json.dumps([message.content for message in primary[0].messages])
    assert "compaction_staging" not in resumed.session.progress
    retained = resumed.session.progress["task_material"]
    assert bool(retained.get("paid_observations")) is not invalid_final
    assert "paid_source_ref" in retained
    if not same_activation:
        async with database.sessions() as reader:
            state = await reader.scalar(select(inputs.c.state).where(inputs.c.id == pending))
        assert state == "pending"
        await control.repository.release(lease)
        lease = await control.repository.acquire(control.lease.conversation_id, 1)
        resumed = WorkControl(
            control.repository, lease, control.source_key, control.source, AsyncMock()
        )
        resumed.current = await control.repository.get(control.current["id"])
        await runner.run(initial, replace(runtime, work_control=resumed), None)
    primary = [request for request in provider.requests if not request.structured_output]
    assert len(primary) == 2
    assert sum((message.content or "").count(text) for message in primary[-1].messages) == 1
    assert len([request for request in provider.requests if request.structured_output]) == 1
    assert (await control.repository.get(control.current["id"]))[
        "model_requests"
    ] == before_budget + 2
    async with database.sessions() as reader:
        assert (
            await reader.scalar(select(inputs.c.state).where(inputs.c.id == pending)) == "consumed"
        )
    await control.repository.release(lease)


async def test_dispatched_replay_keeps_original_opaque_sequence_before_pending_input(
    database, tmp_path
):
    control, session, initial = await _session(database, tmp_path)
    provider = FakeLLMProvider(lambda request: "Complete response at the original boundary.")
    runner, runtime = await _runtime(
        database, control, initial, provider, contract_workspace=tmp_path / "main-contract"
    )
    runtime, _ = await _seed_runner_contract(runner, runtime, session, initial)
    session.transcript.accept(
        ProviderContinuation(
            provider="gemini",
            protocol="gemini",
            payload=(
                {
                    "role": "model",
                    "parts": [
                        {"text": "Original private tail.", "thoughtSignature": "original-signature"}
                    ],
                },
            ),
        )
    )
    await control.reserve_request()
    await session.save("dispatched")
    original = session.transcript.request()
    pending = await control.repository.enqueue(
        control.lease.conversation_id,
        1,
        "dispatched-pending",
        kind="message",
        event_id=1,
        work_id=control.current["id"],
        ready=False,
    )
    text = "New input must not alter the original dispatched request."
    assert await control.repository.prepare_input(pending, {"text": text})
    await control.repository.release(control.lease)
    lease = await control.repository.acquire(control.lease.conversation_id, 1)
    resumed = WorkControl(
        control.repository, lease, control.source_key, control.source, AsyncMock()
    )
    resumed.current = await control.repository.get(control.current["id"])
    before_budget = resumed.current["model_requests"]
    await runner.run(initial, replace(runtime, work_control=resumed, max_model_requests=2), None)
    assert len(provider.requests) == 2
    replay = provider.requests[0]
    assert replay.messages == original.messages
    assert replay.continuation == original.continuation
    assert replay.continuation_items == original.items
    assert replay.request_chain_id == session.transcript.chain_id
    entries = (*provider.requests[1].messages, *provider.requests[1].continuation_items)
    assert sum((getattr(message, "content", None) or "").count(text) for message in entries) == 1
    assert (await control.repository.get(control.current["id"]))[
        "model_requests"
    ] == before_budget + 2
    async with database.sessions() as reader:
        assert (
            await reader.scalar(select(inputs.c.state).where(inputs.c.id == pending)) == "consumed"
        )
    await control.repository.release(lease)
