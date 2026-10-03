"""Real paired receipts and immutable task anchors survive near-window compaction."""

import json
from dataclasses import asdict, replace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from tests.support.work_compaction import summary_json
from tests.unit.test_work_compaction_capacity import _grow, _runtime, _session, _snapshot, _steer
from tests.unit.test_work_effect_results import owned_session

from qq_ai_bot.capabilities.results import ToolExecutionResult, ToolResultBudgeter
from qq_ai_bot.domain.messages import ChatMessage, ChatRequest, ToolCall, ToolFunction
from qq_ai_bot.llm.fake import FakeLLMProvider
from qq_ai_bot.model_runtime.capacity import ModelCapacity, estimate_request_tokens
from qq_ai_bot.model_runtime.models import ModelExecutionPriority
from qq_ai_bot.runtime.activation_outcome import SegmentBudgetReached
from qq_ai_bot.runtime.protocol_schema import refs as protocol_refs
from qq_ai_bot.runtime.work_compaction import validate_summary
from qq_ai_bot.runtime.work_control import WorkControl
from qq_ai_bot.runtime.work_journal import decode_transcript
from qq_ai_bot.runtime.work_recovery_schema import deliveries
from qq_ai_bot.runtime.work_repository import WorkCapacityError
from qq_ai_bot.runtime.work_schema_v1 import effects
from qq_ai_bot.runtime.work_session import WorkSession
from qq_ai_bot.services.turn_transcript import TurnTranscript


async def _receipts(database, control):
    async with database.sessions() as reader:
        return [
            dict(row)
            for row in (
                await reader.execute(
                    select(effects)
                    .where(effects.c.work_id == control.current["id"])
                    .order_by(effects.c.effect_key)
                )
            ).mappings()
        ]


def _main(transcript):
    return ChatRequest(
        messages=transcript.request().messages,
        model="fixed-profile-model",
        max_output_tokens=8192,
        request_chain_id=transcript.chain_id,
    )


async def _near_window_session(database, tmp_path, *, large_anchor, single_record=False):
    control, session, store = await owned_session(database, tmp_path)
    control.session = session
    # This is the already-compiled current user message: the permission is a
    # durable task requirement, and the large observation is its original data.
    task = ChatMessage(
        "user",
        "Do not publish or resend any delivered file.\nCompiled original observation:\n"
        + "original-scoped-data " * (11000 if large_anchor else 200),
    )
    initial = (ChatMessage("system", "fixed system contract"), task)
    await session.restore(TurnTranscript(initial), compaction_brief=task)
    capacity = ModelCapacity(context_tokens=128000, input_tokens=128000)
    budget = capacity.input_budget(128000, output_tokens=8192)
    calls = []
    invocations = []
    for index in range(2 if single_record else 28):
        call = ToolCall(f"read-{index}", ToolFunction("read_file", '{"path":"source"}'))
        outcome = ToolExecutionResult(
            ok=True,
            data={"record": index, "output": "evidence " * (100 if large_anchor else 1000)},
            provider_id="core",
            tool_name=call.function.name,
            mutation_committed=False,
        )

        async def render(outcome=outcome):
            return (
                await ToolResultBudgeter(max_characters=None, artifacts=store).render(outcome)
            ).text

        invoke = AsyncMock(side_effect=render)
        calls.append(call)
        invocations.append(invoke)
        session.transcript.append(ChatMessage("assistant", "Read evidence", tool_calls=(call,)))
        result = await session.execute(call, invoke, side_effecting=False)
        session.transcript.append_result(call.id, result)
        session.progress.setdefault("model_observations", []).append(
            {
                "sequence": index,
                "content": "Read evidence",
                "tool_calls": [asdict(call)],
                "results": [{"call_id": call.id, "output": result, "executed": True}],
            }
        )
    # Reach the production trigger with exact complete-request accounting.
    # Put replaceable history before the latest paired tool suffix.
    target = int(budget * 0.96)
    low, high = 0, budget * 4
    original = session.transcript.request().messages
    while low < high:
        middle = (low + high) // 2
        messages = (
            *initial,
            ChatMessage("assistant", "old history " + "x" * middle),
            *original[2:],
        )
        size = estimate_request_tokens(replace(_main(session.transcript), messages=messages))
        if size < target:
            low = middle + 1
        else:
            high = middle
    original_chain = session.transcript.chain_id
    session.transcript = TurnTranscript(
        (*initial, ChatMessage("assistant", "old history " + "x" * low), *original[2:])
    )
    # Keep the original call key / protocol chain identity, including all effects.
    session.transcript.chain_id = original_chain
    await session.save("paired")
    assert budget * 0.90 < estimate_request_tokens(_main(session.transcript)) < budget
    return control, session, initial, capacity, budget, calls, invocations


@pytest.mark.parametrize("large_anchor", [False, True])
async def test_actual_near_window_compaction_keeps_anchor_receipts_and_bounded_requests(
    database, tmp_path, large_anchor
):
    control, session, initial, capacity, budget, calls, invocations = await _near_window_session(
        database, tmp_path, large_anchor=large_anchor
    )
    original_request = _main(session.transcript)
    original_request_for_transcript = session.transcript.request()
    original_receipts = await _receipts(database, control)
    original_work = await control.repository.get(control.current["id"])
    original_journal = await _snapshot(database, control.current["id"])
    if large_anchor:
        assert estimate_request_tokens(replace(original_request, messages=initial)) > budget * 0.50
    auxiliary_requests = []

    def summarize(request):
        auxiliary_requests.append(request)
        source = json.loads(request.messages[-1].content)
        return summary_json(source)

    runner, runtime = await _runtime(
        database,
        control,
        initial,
        FakeLLMProvider(summarize),
        work_compaction_max_output_tokens=32768,
    )
    runner._models.capacity = lambda _task: capacity
    candidate = await runner._compact_work(
        runtime, ModelExecutionPriority.FOREGROUND, budget, original_request
    )
    assert auxiliary_requests
    measurements = [
        (
            estimate_request_tokens(request),
            capacity.input_budget(128000, output_tokens=request.max_output_tokens),
        )
        for request in auxiliary_requests
    ]
    assert all(size <= limit for size, limit in measurements), measurements
    assert candidate.chain_id != original_journal["chain_id"]
    assert candidate.request().messages[:2] == initial
    candidate_request = replace(original_request, messages=candidate.request().messages)
    assert estimate_request_tokens(candidate_request) < estimate_request_tokens(original_request)
    assert estimate_request_tokens(candidate_request) <= budget
    capsule = json.loads(candidate.request().messages[-1].content)
    retained = capsule["recent_raw_records"]
    for record in retained:
        for call in record.get("tool_calls", []):
            assert any(item.get("tool_call_id") == call["id"] for item in retained)
    assert await _receipts(database, control) == original_receipts
    current = await control.repository.get(control.current["id"])
    assert current["id"] == original_work["id"]
    assert current["tool_calls"] == original_work["tool_calls"]
    assert current["model_requests"] == original_work["model_requests"] + len(auxiliary_requests)
    for invoke in invocations:
        invoke.assert_awaited_once()
    restored = WorkSession(control, session.contract)
    control.session = restored
    replay = await restored.restore(TurnTranscript((ChatMessage("user", "unused new wakeup"),)))
    assert replay.request().messages[0].content == "unused new wakeup"
    assert not restored.uses_recovery_transcript
    material = json.loads(replay.request().messages[-1].content)
    assert material["goal"] == original_work["goal"]
    assert await _receipts(database, control) == original_receipts
    original_key = next(
        receipt["effect_key"]
        for receipt in original_receipts
        if receipt["effect_key"].endswith(":" + calls[-1].id)
    )
    assert await restored.journal.effect_result(original_key) == next(
        record.content
        for record in original_request.messages
        if record.tool_call_id == calls[-1].id
    )
    invocations[-1].assert_awaited_once()
    old_manifest = await restored.journal.objects.get(capsule["previous_protocol_ref"])
    old_protocol = await restored.journal.objects.hydrate(old_manifest)
    assert (
        decode_transcript(old_protocol["transcript"]).request() == original_request_for_transcript
    )
    assert await _receipts(database, control) == original_receipts
    assert (
        original_journal["payload_json"]
        != (await _snapshot(database, control.current["id"]))["payload_json"]
    )


async def test_large_public_record_pages_resume_without_repaying_the_first_page(database, tmp_path):
    control, session, initial, capacity, budget, _, invocations = await _near_window_session(
        database, tmp_path, large_anchor=False, single_record=True
    )
    original = session.transcript.request()
    original_records = json.loads(json.dumps(session.public_records()))
    receipts = await _receipts(database, control)
    requests = []
    pages = []

    def summarize(request):
        requests.append(request)
        source = json.loads(request.messages[-1].content)
        pages.append(source)
        return summary_json(source)

    runner, runtime = await _runtime(
        database,
        control,
        initial,
        FakeLLMProvider(summarize),
        work_compaction_max_output_tokens=32768,
    )
    runner._models.capacity = lambda _task: capacity
    control.segment_model_limit = 1
    main = _main(session.transcript)
    with pytest.raises(SegmentBudgetReached):
        await runner._compact_work(runtime, ModelExecutionPriority.FOREGROUND, budget, main)
    assert len(pages) == 1
    assert pages[0]["source_fragments"]
    assert session.transcript.request() == original
    persisted = session.progress["compaction_staging"]["source"]["paging"]
    assert persisted["cursor"] == pages[0]["paging"]["next_cursor"]
    async with database.sessions() as reader:
        assert (
            await reader.scalar(
                select(protocol_refs.c.sha256).where(
                    protocol_refs.c.work_id == control.current["id"],
                    protocol_refs.c.sha256 == persisted["snapshot_ref"],
                )
            )
            == persisted["snapshot_ref"]
        )
    await session.journal.objects.cleanup(grace_seconds=0)
    assert (await session.journal.objects.get(persisted["snapshot_ref"]))["units"]
    await control.repository.release(control.lease)
    lease = await control.repository.acquire(control.lease.conversation_id, 1)

    async def validate():
        assert await control.repository.valid(lease)

    resumed_control = WorkControl(
        control.repository, lease, control.source_key, control.source, validate
    )
    resumed_control.current = await control.repository.get(control.current["id"])
    resumed = WorkSession(resumed_control, session.contract)
    resumed_control.session = resumed
    await resumed.restore(TurnTranscript((ChatMessage("user", "new wakeup is not a new goal"),)))
    assert resumed.transcript.request() == original
    candidate = await runner._compact_work(
        replace(runtime, work_control=resumed_control),
        ModelExecutionPriority.FOREGROUND,
        budget,
        main,
    )
    assert len(pages) > 1
    assert pages[1]["paging"]["cursor"] == persisted["cursor"]
    assert all(page["paging"]["snapshot_ref"] == persisted["snapshot_ref"] for page in pages)
    assert len({tuple(page["paging"]["cursor"]) for page in pages}) == len(pages)
    # Reconstruct every original public record, including the oversized JSON
    # record, from the exact ordered fragment offsets rather than summary text.
    collected = {}
    fragments = {}
    for page in pages:
        for index, record in zip(
            page.get("record_source_indices", []), page.get("records", []), strict=True
        ):
            assert index not in collected
            collected[index] = record
        for fragment in page.get("source_fragments", []):
            if fragment["kind"] == "records":
                fragments.setdefault(int(fragment["ref"].split(":")[1]), []).append(fragment)
    for index, chunks in fragments.items():
        text = ""
        for chunk in chunks:
            assert chunk["offset"] == len(text)
            text += chunk["text"]
        assert len(text) == chunks[-1]["total_characters"]
        collected[index] = json.loads(text)
    assert [collected[index] for index in range(len(original_records))] == original_records
    assert all(
        estimate_request_tokens(request)
        <= capacity.input_budget(128000, output_tokens=request.max_output_tokens)
        for request in requests
    )
    assert candidate.request().messages[:2] == initial
    assert await _receipts(database, resumed_control) == receipts
    current = await control.repository.get(control.current["id"])
    assert current["model_requests"] == len(pages)
    for invoke in invocations:
        invoke.assert_awaited_once()
    assert "compaction_staging" not in resumed.progress


@pytest.mark.parametrize(
    ("code", "phrase"),
    [
        ("work_compaction_no_capacity_improvement", "上下文压缩"),
        ("work_compaction_source_capacity", "用于压缩的资料"),
        ("work_protocol_storage_capacity", "存储空间"),
    ],
)
async def test_actual_capacity_pause_notices_keep_distinct_causes(database, tmp_path, code, phrase):
    control, session, _ = await owned_session(database, tmp_path)
    await session.save("paired")
    await control.recover_failure(WorkCapacityError(code))
    current = await control.repository.get(control.current["id"])
    assert current["state"] == "suspended" and current["reason"] == code
    async with database.sessions() as reader:
        notice = (
            (
                await reader.execute(
                    select(deliveries).where(
                        deliveries.c.work_id == current["id"], deliveries.c.kind == "notice"
                    )
                )
            )
            .mappings()
            .one()
        )
    text = json.loads(notice["payload_json"])["text"]
    assert phrase in text and "已暂停并保留已有结果" in text
    if code != "work_protocol_storage_capacity":
        assert "存储" not in text and "工作记录容量不足" not in text


async def test_less_than_ten_percent_reduction_is_usable_above_soft_target(database, tmp_path):
    _, session, _ = await _session(database, tmp_path)
    task = ChatMessage("user", "Exact original observation " + "fixed " * 35000)
    initial = (ChatMessage("system", "fixed contract"), task)
    await session.restore(TurnTranscript(initial), compaction_brief=task)
    session.transcript.append(ChatMessage("assistant", "replaceable " + "x" * 18000))
    await session.save("paired")
    before = _main(session.transcript)
    original_size = estimate_request_tokens(before)
    source = await session.summary_source()
    candidate = await session.compact(
        summary_json(source), target_tokens=40000, ceiling_tokens=119808, request_template=before
    )
    size = estimate_request_tokens(replace(before, messages=candidate.request().messages))
    assert original_size * 0.9 < size < original_size
    assert 40000 < size < 119808
    assert candidate.request().messages[:2] == initial
    assert session.progress["compaction_request_tokens"] == size
    assert len(session.progress["chain_links"]) == 1


async def test_forty_real_directives_and_large_valid_summary_survive_paginated_compaction(
    database, tmp_path
):
    control, session, initial = await _session(database, tmp_path)
    texts = [f"Requirement {index}: " + "preserve " * 240 for index in range(40)]
    identities = [await _steer(control, session, index, text) for index, text in enumerate(texts)]
    _grow(session.transcript)
    await session.save("paired")
    before = _main(session.transcript)
    pages, responses = [], []

    def summarize(request):
        source = json.loads(request.messages[-1].content)
        pages.append(source)
        result = summary_json(source)
        responses.append(result)
        return result

    capacity = ModelCapacity(context_tokens=128000, input_tokens=128000)
    runner, runtime = await _runtime(
        database,
        control,
        initial,
        FakeLLMProvider(summarize),
        work_compaction_max_output_tokens=32768,
    )
    runner._models.capacity = lambda _task: capacity
    candidate = await runner._compact_work(
        runtime, ModelExecutionPriority.FOREGROUND, 119808, before
    )
    assert len(responses[-1].encode()) > 65536
    material = session.progress["task_material"]
    assert [fact["text"] for fact in material["directives"]] == texts
    assert material["covered_input_id"] == identities[-1]
    assert candidate.request().messages[:2] == initial
    restarted = WorkSession(control, session.contract)
    control.session = restarted
    await restarted.restore(TurnTranscript((ChatMessage("user", "restart"),)))
    assert restarted.progress["task_material"] == material
    assert restarted.transcript.request().messages[0].content == "restart"
    assert not restarted.uses_recovery_transcript
    restored_material = json.loads(restarted.transcript.request().messages[-1].content)
    assert restored_material["task_material"] == material
    invalid = json.loads(responses[-1])
    invalid["pending"][0]["refs"] = ["record:not-in-the-source"]
    with pytest.raises(WorkCapacityError, match="work_compaction_invalid_reference"):
        validate_summary(json.dumps(invalid), pages[-1])


async def test_paid_final_page_keeps_exact_validation_scope_when_new_aux_window_is_smaller(
    database, tmp_path, monkeypatch
):
    control, session, initial = await _session(database, tmp_path)
    _grow(session.transcript)
    await session.save("paired")
    original = session.transcript.request()
    chain = session.transcript.chain_id
    save = session.save

    async def fail_candidate(phase, calls=(), **kwargs):
        if session.transcript.chain_id != chain:
            raise WorkCapacityError("candidate_save_interrupted")
        return await save(phase, calls, **kwargs)

    monkeypatch.setattr(session, "save", fail_candidate)
    requests = []

    def summarize(request):
        requests.append(request)
        return summary_json(request.messages[-1].content)

    runner, runtime = await _runtime(database, control, initial, FakeLLMProvider(summarize))
    main = _main(session.transcript)
    with pytest.raises(WorkCapacityError, match="candidate_save_interrupted"):
        await runner._compact_work(runtime, ModelExecutionPriority.FOREGROUND, 119808, main)
    assert session.compaction_ready_summary is not None
    paid_requests = len(requests)
    staging = session.progress["compaction_staging"]
    source = staging["source"]
    assert source["paging"]["next_cursor"][0] == source["paging"]["total_units"]
    resumed = WorkSession(control, session.contract)
    control.session = resumed
    await resumed.restore(TurnTranscript((ChatMessage("user", "restart"),)))
    assert resumed.transcript.request() == original
    # Capacity policy changed after this final auxiliary response was paid.
    # It need not be sent again: only the new main candidate still has to fit.
    loaded = json.loads(await resumed.summary_source(fits=lambda _: False))
    assert loaded == source
    candidate = await resumed.compact(
        resumed.compaction_ready_summary,
        target_tokens=59904,
        ceiling_tokens=119808,
        request_template=main,
    )
    assert len(requests) == paid_requests
    assert candidate.chain_id != chain
    assert candidate.request().messages[:2] == initial
    assert estimate_request_tokens(replace(main, messages=candidate.request().messages)) <= 119808
