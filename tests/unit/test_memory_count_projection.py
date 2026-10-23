"""Writer fact projections count actual readable sources without rendering evidence."""

from datetime import UTC, datetime

from sqlalchemy import insert
from tests.support.self_initiative_memory_quality_helpers import reflection_fact
from tests.unit.test_memory_v2 import _append_event

from yuki.memory.models import MemoryEvidenceCreate, MemoryFactCreate
from yuki.memory.repository import MemoryFactRepository
from yuki.memory.service import MemoryFactService
from yuki.persistence.models import (
    ChatEventModel,
    MemoryEvidenceModel,
    MemoryToolReceiptModel,
)
from yuki.persistence.repositories import EventLedgerRepository


async def test_missing_writer_counts_use_current_sql_readability_without_evidence_dtos(
    database, monkeypatch
):
    repository = MemoryFactRepository(database)
    facts = MemoryFactService(repository)
    ledger = EventLedgerRepository(database)
    source = await _append_event(ledger, message_id="count-source", content="original source")
    hidden = await _append_event(ledger, message_id="hidden-source", content="hidden source")
    next_source = await _append_event(ledger, message_id="next-source", content="new source")
    first = await facts.remember(
        MemoryFactCreate(
            scope_type="person",
            subject_user_id="1001",
            memory_key="count:original",
            category="test",
            content="original fact",
            source_type="automatic",
        ),
        evidence=MemoryEvidenceCreate(
            event_id=source.id,
            source_speaker_user_id="1001",
            relation="confirmation",
            confidence=0.3,
            authority="self_report",
            excerpt="original source",
        ),
    )
    ids = [first.id]
    async with database.immediate_session() as writer:
        hidden_row = await writer.get(ChatEventModel, hidden.id)
        hidden_row.utterance_fingerprint = "a" * 64
        hidden_row.suppression_status = "duplicate"
        await writer.execute(
            insert(MemoryEvidenceModel),
            [
                dict(
                    fact_id=identity,
                    event_id=event_id,
                    source_speaker_user_id="1001",
                    relation="confirmation",
                    confidence=0.3,
                    authority="self_report",
                    excerpt="retained source body",
                    created_at=datetime.now(UTC),
                )
                for identity in ids
                for event_id in (source.id, hidden.id)
                if identity != first.id or event_id != source.id
            ],
        )
    # This is the same empty preparation used by a writer that discovers a fact
    # during its current selector query. Only a count is requested at this point.
    prepared = await facts.prepare_write(())
    rendered_in_writer = []
    convert = repository._evidence_record

    def observed(row):
        if database._writer.locked():
            rendered_in_writer.append(row.id)
        return convert(row)

    monkeypatch.setattr(repository, "_evidence_record", observed)
    async with repository.write_session(prepared) as writer:
        projected = await repository.get_facts(tuple(ids), session=writer)
        assert {fact.id: fact.evidence_count for fact in projected} == dict.fromkeys(ids, 1)
        confirmed = await facts.confirm_fact(
            ids[-1],
            MemoryEvidenceCreate(
                event_id=next_source.id,
                source_speaker_user_id="1001",
                relation="confirmation",
                confidence=0.3,
                authority="self_report",
                excerpt="new source",
            ),
            session=writer,
        )
        assert confirmed.evidence_count == 2
    assert rendered_in_writer == []
    assert len(await repository.list_evidence(ids[-1], limit=None)) == 2


async def test_prepared_evidence_keeps_original_order_limits_and_same_writer_additions(database):
    repository = MemoryFactRepository(database)
    facts = MemoryFactService(repository)
    ledger = EventLedgerRepository(database)
    first = await _append_event(ledger, message_id="prepared-first", content="first")
    later = await _append_event(ledger, message_id="prepared-later", content="later")
    fact = await facts.remember(
        MemoryFactCreate(
            scope_type="person",
            subject_user_id="1001",
            memory_key="prepared:evidence",
            category="test",
            content="prepared fact",
            source_type="automatic",
        ),
        evidence=MemoryEvidenceCreate(
            event_id=first.id,
            source_speaker_user_id="1001",
            relation="confirmation",
            authority="self_report",
            confidence=0.3,
            excerpt="first",
        ),
    )
    prepared = await facts.prepare_write((fact.id,))
    async with repository.write_session(prepared) as writer:
        assert await repository.add_evidence(
            fact.id,
            MemoryEvidenceCreate(
                event_id=later.id,
                source_speaker_user_id="1001",
                relation="confirmation",
                authority="self_report",
                confidence=0.3,
                excerpt="later",
            ),
            session=writer,
        )
        all_rows = await repository.list_evidence(fact.id, limit=None, session=writer)
        assert [row.excerpt for row in all_rows] == ["later", "first"]
        assert await repository.list_evidence(fact.id, limit=1, session=writer) == all_rows[:1]
        assert (await facts.get_fact(fact.id, session=writer)).evidence_count == 2
    assert await repository.list_evidence(fact.id, limit=None) == all_rows


async def test_missing_writer_counts_preserve_self_receipt_source_proof(database):
    facts, _source, _run_id, batch, fact_id = await reflection_fact(database)
    expected = await facts.repository.list_evidence(fact_id, limit=None)
    prepared = await facts.prepare_write(())
    async with facts.repository.write_session(prepared) as writer:
        assert (await facts.get_fact(fact_id, session=writer)).evidence_count == len(expected)
    async with database.immediate_session() as writer:
        receipt = await writer.get(MemoryToolReceiptModel, batch.first_receipt_id)
        receipt.result_excerpt = "source was erased"
    prepared = await facts.prepare_write(())
    async with facts.repository.write_session(prepared) as writer:
        assert (await facts.get_fact(fact_id, session=writer)).evidence_count == 0
