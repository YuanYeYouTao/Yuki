"""Real Memory writes retain canonical owners and original operation receipts."""

import json
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select
from tests.unit.test_memory_mutation import _context, _event, _service

from qq_ai_bot.domain.audio import AudioTranscript, serialize_transcripts
from qq_ai_bot.memory.claim_processor import MemoryProcessingContext
from qq_ai_bot.memory.enums import MemoryStatus
from qq_ai_bot.memory.extraction import MemoryClaim
from qq_ai_bot.memory.mutation.models import MemoryMutationRequest
from qq_ai_bot.memory.quality.audit import MemoryProductionQualityAudit
from qq_ai_bot.memory.quality.hygiene import MemoryProvenanceHygiene
from qq_ai_bot.memory.repository import MemoryJobRepository
from qq_ai_bot.memory.subjects import SubjectContextBuilder
from qq_ai_bot.persistence.models import MemoryMutationReceiptModel


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
    conflict = await mutations.mutate(create_request("喜欢咖啡"), first_context)
    assert not conflict.ok and conflict.reason_code == "memory_operation_payload_conflict"
    async with database.sessions() as reader:
        assert await reader.scalar(select(func.count(MemoryMutationReceiptModel.id))) == 3


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
