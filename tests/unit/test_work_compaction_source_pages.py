"""Complete-request source pages preserve original references through partial recovery."""

import json
from dataclasses import replace

import pytest
from sqlalchemy import select
from tests.support.work_compaction import summary_json
from tests.unit.test_work_compaction_capacity import _runtime, _session, _snapshot

from qq_ai_bot.domain.messages import ChatMessage, ChatRequest
from qq_ai_bot.llm.fake import FakeLLMProvider
from qq_ai_bot.model_runtime.capacity import ModelCapacity, estimate_request_tokens
from qq_ai_bot.model_runtime.models import ModelExecutionPriority
from qq_ai_bot.runtime.protocol_schema import refs
from qq_ai_bot.runtime.work_repository import WorkCapacityError
from qq_ai_bot.runtime.work_session import WorkSession
from qq_ai_bot.services.turn_transcript import TurnTranscript


def _fits(budget):
    template = ChatRequest(
        messages=(ChatMessage("system", "fixed summary contract"),), model="test"
    )

    def fits(source):
        return (
            estimate_request_tokens(
                replace(template, messages=(*template.messages, ChatMessage("user", source)))
            )
            <= budget
        )

    return fits


@pytest.mark.asyncio
async def test_fragmented_original_record_recovers_partial_page_without_changing_main_chain(
    database, tmp_path
):
    control, session, initial = await _session(database, tmp_path)
    text = 'Original complete evidence with escaped "fields" and 中文。' * 700
    session.transcript.append(ChatMessage("assistant", text))
    await session.save("paired")
    original_chain = session.transcript.chain_id
    expected_record = json.dumps(session.public_records()[2], ensure_ascii=False)
    fits = _fits(4200)
    source = await session.summary_source(fits=fits)
    pieces = []
    pages = 0
    snapshot_ref = json.loads(source)["paging"]["snapshot_ref"]
    while True:
        assert fits(source)
        payload = json.loads(source)
        for fragment in payload.get("source_fragments", []):
            if fragment["ref"] == "record:2":
                pieces.append(fragment)
        raw = summary_json(source)
        following = await session.next_summary_source(raw, fits=fits)
        await session.stage_compaction(raw if following is None else None)
        pages += 1
        if following is None:
            break
        if pages == 1:
            restored = WorkSession(control, "capacity-contract")
            control.session = restored
            await restored.restore(TurnTranscript(initial), compaction_brief=initial[-1])
            assert restored.transcript.chain_id == original_chain
            assert restored.transcript.request() == session.transcript.request()
            resumed = await restored.summary_source(fits=fits)
            assert resumed == following
            session = restored
            source = resumed
        else:
            source = following
        assert pages < 100
    assert pages > 2
    offset = 0
    for fragment in pieces:
        assert fragment["offset"] == offset
        offset += len(fragment["text"])
        assert fragment["total_characters"] == len(expected_record)
    assert "".join(fragment["text"] for fragment in pieces) == expected_record
    assert session.transcript.chain_id == original_chain
    assert control.current["model_requests"] == control.current["tool_calls"] == 0
    async with database.sessions() as reader:
        assert (
            await reader.scalar(
                select(refs.c.sha256).where(
                    refs.c.work_id == control.current["id"], refs.c.sha256 == snapshot_ref
                )
            )
            == snapshot_ref
        )


@pytest.mark.asyncio
async def test_source_pages_shrink_whole_input_batches_to_actual_request_budget(database, tmp_path):
    control, session, _initial = await _session(database, tmp_path)
    ids = []
    for index in range(9):
        identity = await control.repository.enqueue(
            control.lease.conversation_id,
            1,
            f"event:budget-page-{index}",
            kind="message",
            event_id=1,
            work_id=control.current["id"],
            ready=False,
        )
        assert await control.repository.prepare_input(
            identity, {"text": f"context-{index}:" + "x" * 3000}
        )
        ids.append(identity)
    for index in range(9):
        attempt = f"page-inputs-{index}"
        messages = await control.take_inputs(attempt)
        if not messages:
            break
        for message in messages:
            session.transcript.append(message)
        await session.save("paired")
        await control.repository.consume(control.lease, attempt)
    fits = _fits(7500)
    source = await session.summary_source(fits=fits)
    shown = []
    pages = 0
    while True:
        assert fits(source)
        payload = json.loads(source)
        shown.extend(item["input_id"] for item in payload["task_inputs"])
        raw = json.loads(summary_json(source))
        raw["task_directives"] = []
        for disposition in raw["input_dispositions"]:
            disposition["kind"] = "context"
        next_source = await session.next_summary_source(json.dumps(raw), fits=fits)
        pages += 1
        if next_source is None:
            break
        source = next_source
        assert pages < 30
    assert shown == ids
    assert pages > 1


@pytest.mark.asyncio
async def test_unfit_mandatory_source_preserves_last_paired_journal(database, tmp_path):
    control, session, _initial = await _session(database, tmp_path)
    await session.save("paired")
    original = await _snapshot(database, control.current["id"])
    with pytest.raises(WorkCapacityError, match="work_compaction_source_capacity"):
        await session.summary_source(fits=lambda _source: False)
    assert await _snapshot(database, control.current["id"]) == original
    assert control.current["model_requests"] == control.current["tool_calls"] == 0


@pytest.mark.asyncio
async def test_paid_page_is_saved_before_next_page_base_capacity_failure(database, tmp_path):
    control, session, initial = await _session(database, tmp_path)
    session.transcript.append(ChatMessage("assistant", "Original complete evidence. " * 9000))
    await session.save("paired")
    original = session.transcript.request()
    original_chain = session.transcript.chain_id
    paid_fact = "Retain this verified observation from the paid page. " * 1500
    requests = []
    sources = []

    def summarize(request):
        requests.append(request)
        source = json.loads(request.messages[-1].content)
        sources.append(source)
        return summary_json(source, paid_fact if len(requests) == 1 else "Continue the task.")

    runner, runtime = await _runtime(database, control, initial, FakeLLMProvider(summarize))
    runner._models.capacity = lambda _: ModelCapacity(input_tokens=6500)
    main = ChatRequest(messages=original.messages, max_output_tokens=8192)
    with pytest.raises(WorkCapacityError, match="work_compaction_source_capacity"):
        await runner._compact_work(runtime, ModelExecutionPriority.FOREGROUND, 200000, main)
    assert len(requests) == 1
    assert estimate_request_tokens(requests[0]) <= 6500
    staging = session.progress["compaction_staging"]["source"]
    assert staging["paging"]["cursor"] == sources[0]["paging"]["next_cursor"]
    assert staging["paging"]["cursor"] != sources[0]["paging"]["cursor"]
    assert staging["derived_observations"]["pending"][0]["text"] == paid_fact
    assert session.transcript.request() == original
    saved = await _snapshot(database, control.current["id"])
    assert saved["phase"] == "paired" and saved["chain_id"] == original_chain
    assert control.current["model_requests"] == 1

    resumed = WorkSession(control, session.contract)
    control.session = resumed
    await resumed.restore(TurnTranscript(initial), compaction_brief=initial[-1])
    assert resumed.progress["compaction_staging"]["source"] == staging
    with pytest.raises(WorkCapacityError, match="work_compaction_source_capacity"):
        await runner._compact_work(runtime, ModelExecutionPriority.FOREGROUND, 200000, main)
    assert len(requests) == control.current["model_requests"] == 1
    assert resumed.transcript.request() == original

    # More actual auxiliary capacity allows the already-validated next cursor
    # to proceed; it never asks for the preceding paid page again.
    runner._models.capacity = lambda _: ModelCapacity(input_tokens=100000)
    candidate = await runner._compact_work(runtime, ModelExecutionPriority.FOREGROUND, 200000, main)
    assert sources[1]["paging"]["cursor"] == staging["paging"]["cursor"]
    assert len({tuple(source["paging"]["cursor"]) for source in sources}) == len(sources)
    assert candidate.chain_id != original_chain
    assert candidate.request().messages[:2] == initial
    assert control.current["model_requests"] == len(requests)
