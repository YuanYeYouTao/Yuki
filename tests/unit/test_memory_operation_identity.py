"""Real Memory writes retain canonical owners and original operation receipts."""

import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select
from tests.unit.test_memory_mutation import _context, _event, _service

from yuki.domain.audio import AudioTranscript, serialize_transcripts
from yuki.memory.claim_processor import MemoryProcessingContext
from yuki.memory.enums import MemoryStatus
from yuki.memory.extraction import MemoryClaim
from yuki.memory.mutation.models import MemoryMutationRequest, MemoryMutationResult
from yuki.memory.mutation.repository import MemoryMutationReceiptRepository
from yuki.memory.quality.audit import MemoryProductionQualityAudit
from yuki.memory.quality.hygiene import MemoryProvenanceHygiene
from yuki.memory.repository import MemoryJobRepository
from yuki.memory.subjects import SubjectContextBuilder
from yuki.persistence.models import MemoryMutationReceiptModel


def create_request(content="喜欢茶"):
    return MemoryMutationRequest.model_validate(
        {
            "operation": "create",
            "target": {"subject_ref": "current_speaker", "scope_type": "person"},
            "new_content": content,
            "memory_key": "preference:tea",
            "category": "preference",
        }
    )


async def test_operation_replay_is_distinct_from_equal_content_and_later_actions(database):
    mutations, facts, ledger, _ = _service(database)
    event = await _event(ledger, message_id="original-ops", sender_user_id="1001", content="喜欢茶")
    first_context = replace(_context(event), operation_id="original:create:first")
    second_context = replace(_context(event), operation_id="original:create:second")
    first = await mutations.mutate(create_request(), first_context)
    second = await mutations.mutate(create_request(), second_context)
    assert first.ok and second.ok and first.new_fact_id != second.new_fact_id
    assert first.mutation_id != second.mutation_id
    invalidated = await mutations.mutate(
        MemoryMutationRequest(operation="invalidate", fact_id=first.new_fact_id),
        replace(_context(event), operation_id="original:invalidate"),
    )
    assert invalidated.ok and not invalidated.deduplicated
    assert (await facts.get_fact(first.new_fact_id)).status is MemoryStatus.INVALIDATED
    replay = await mutations.mutate(create_request(), first_context)
    assert replay.ok and replay.deduplicated
    assert replay.mutation_id == first.mutation_id and replay.new_fact_id == first.new_fact_id
    assert (await facts.get_fact(first.new_fact_id)).status is MemoryStatus.INVALIDATED
    receipts = MemoryMutationReceiptRepository(database)
    async with database.read_session() as reader:
        receipt = await receipts.get(first.mutation_id, session=reader)
    assert receipt is not None
    recovered = MemoryMutationResult.from_receipt(receipt, deduplicated=False)
    assert recovered.tool_payload() == first.tool_payload()
    assert recovered.outcome.value == "committed" and not recovered.deduplicated
    assert await receipts.get("missing-mutation") is None
    conflict = await mutations.mutate(create_request("喜欢咖啡"), first_context)
    assert not conflict.ok and conflict.reason_code == "memory_operation_payload_conflict"
    async with database.read_session() as reader:
        assert await reader.scalar(select(func.count(MemoryMutationReceiptModel.id))) == 3


@pytest.mark.parametrize("lost_ack", [False, True])
async def test_concurrent_original_operation_rejects_a_different_payload(
    database, monkeypatch, lost_ack
):
    mutations, facts, ledger, _ = _service(database)
    event = await _event(
        ledger, message_id="concurrent-original-payload", sender_user_id="1001", content="喜欢茶"
    )
    context = replace(_context(event), operation_id="original:concurrent:create")
    both_prepared = asyncio.Event()
    original_prepare = facts.prepare_write
    arrivals = 0

    async def prepare(*args, **kwargs):
        nonlocal arrivals
        result = await original_prepare(*args, **kwargs)
        arrivals += 1
        if arrivals == 2:
            both_prepared.set()
        await asyncio.wait_for(both_prepared.wait(), 3)
        return result

    monkeypatch.setattr(facts, "prepare_write", prepare)
    if lost_ack:
        original_writer = facts.repository.write_session

        @asynccontextmanager
        async def lose_ack(*args, **kwargs):
            async with original_writer(*args, **kwargs) as writer:
                yield writer
            raise RuntimeError("commit acknowledgement lost")

        monkeypatch.setattr(facts.repository, "write_session", lose_ack)
    requests = (create_request("喜欢茶"), create_request("喜欢咖啡"))
    results = await asyncio.gather(*(mutations.mutate(request, context) for request in requests))
    winner = next(index for index, result in enumerate(results) if result.ok)
    loser = results[1 - winner]
    assert not loser.ok and loser.reason_code == "memory_operation_payload_conflict"
    assert len(await facts.list_person("1001")) == 1
    original = results[winner]
    replay = await mutations.mutate(requests[winner], context)
    assert replay.ok and replay.deduplicated
    assert (replay.mutation_id, replay.new_fact_id) == (original.mutation_id, original.new_fact_id)
    assert (await facts.get_fact(replay.new_fact_id)).content == requests[winner].new_content
    async with database.read_session() as reader:
        assert await reader.scalar(select(func.count(MemoryMutationReceiptModel.id))) == 1


async def test_prepared_operation_checks_the_receipt_that_arrived_before_its_writer(
    database, monkeypatch
):
    mutations, facts, ledger, _ = _service(database)
    event = await _event(
        ledger, message_id="prepared-original-payload", sender_user_id="1001", content="喜欢茶"
    )
    context = replace(_context(event), operation_id="original:prepared:create")
    pending_request = create_request("喜欢咖啡")
    original_prepare = mutations._prepare
    prepared, committed = asyncio.Event(), asyncio.Event()

    async def prepare(request, *args, **kwargs):
        result = await original_prepare(request, *args, **kwargs)
        if request == pending_request:
            prepared.set()
            await committed.wait()
        return result

    monkeypatch.setattr(mutations, "_prepare", prepare)
    pending = asyncio.create_task(mutations.mutate(pending_request, context))
    await asyncio.wait_for(prepared.wait(), 3)
    try:
        original = await mutations.mutate(create_request(), context)
        assert original.ok
    finally:
        committed.set()
    result = await pending
    assert not result.ok and result.reason_code == "memory_operation_payload_conflict"
    assert [row.id for row in await facts.list_person("1001")] == [original.new_fact_id]


@pytest.mark.parametrize("pretty_json", [False, True])
async def test_current_voice_is_manual_and_automatic_evidence_while_reply_voice_is_not(
    database, pretty_json
):
    mutations, facts, ledger, processor = _service(database)
    event = await _event(ledger, message_id="voice-evidence", sender_user_id="1001", content="")
    transcript = serialize_transcripts(
        (
            AudioTranscript("current", 0, "喜欢茶"),
            AudioTranscript("reply", 0, "喜欢咖啡"),
        )
    )
    if pretty_json:
        transcript = json.dumps(json.loads(transcript), ensure_ascii=False, indent=2)
    assert await ledger.set_audio_transcript(event.id, transcript, generation=1)
    event = await ledger.get_event(event.id)
    assert event is not None and "喜欢咖啡" not in event.evidence_content
    manual = await mutations.mutate(
        create_request().model_copy(update={"evidence_quote": "喜欢茶"}),
        replace(_context(event), operation_id="voice:manual"),
    )
    assert manual.ok
    without_quote = await mutations.mutate(
        create_request(),
        replace(_context(event), operation_id="voice:manual-without-quote"),
    )
    assert without_quote.ok
    evidence = await facts.list_evidence(without_quote.new_fact_id)
    assert [row.event_id for row in evidence] == [event.id]
    assert "喜欢茶" in evidence[0].excerpt and "喜欢咖啡" not in evidence[0].excerpt
    rejected = await mutations.mutate(
        create_request("喜欢咖啡").model_copy(update={"evidence_quote": "喜欢咖啡"}),
        replace(_context(event), operation_id="voice:quoted-other"),
    )
    assert not rejected.ok and rejected.reason_code == "evidence_quote_not_in_current_event"
    jobs = MemoryJobRepository(database)
    assert await jobs.enqueue(event.id, event.scope.key)
    (job,) = await jobs.claim()
    claim = processor.validate(
        MemoryClaim(
            subject_ref="speaker",
            scope_type="person",
            memory_key="tea",
            category="preference",
            content="喜欢茶",
            evidence_quote="喜欢茶",
        ),
        event,
        subject_context=await SubjectContextBuilder(database=database).build(event),
    )
    assert claim is not None
    applied = await mutations.mutate_validated_claim(
        claim, MemoryProcessingContext(event=event), conversation_key=event.scope.key, job=job
    )
    assert applied.ok
    repeated = await mutations.mutate_validated_claim(
        claim, MemoryProcessingContext(event=event), conversation_key=event.scope.key, job=job
    )
    assert repeated.deduplicated and repeated.mutation_id == applied.mutation_id
    audit = await MemoryProductionQualityAudit(database).run()
    assert not any(
        issue.issue_code
        in {"memory_job_source_invalid", "evidence_source_invalid", "evidence_excerpt_missing"}
        for issue in audit.issues
        if issue.count
    )
    hygiene = MemoryProvenanceHygiene(database)
    plan = await hygiene.scan()
    assert manual.new_fact_id not in plan.invalid_fact_ids
    assert without_quote.new_fact_id not in plan.invalid_fact_ids
    await hygiene.apply(plan.fingerprint)
    assert (await facts.get_fact(manual.new_fact_id)).status is MemoryStatus.ACTIVE
    assert (await facts.get_fact(without_quote.new_fact_id)).status is MemoryStatus.ACTIVE
    assert (await facts.get_fact(manual.new_fact_id)).last_confirmed_at <= datetime.now(UTC)
