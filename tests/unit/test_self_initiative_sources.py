"""Existing SELF dependencies retain exact visibility and readable provenance."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, update
from tests.support.self_initiative_memory_quality_helpers import reflection_fact

from qq_ai_bot.memory.self_origin import read_self_initiative_facts
from qq_ai_bot.persistence.models import MemoryEvidenceModel, MemoryFactModel

pytestmark = pytest.mark.asyncio


async def test_older_readable_evidence_survives_newer_unreadable_sources(database):
    from sqlalchemy import insert

    from qq_ai_bot.persistence.models import MemoryToolReceiptModel

    facts, source, _run, batch, fact_id = await reflection_fact(database)
    now = datetime.now(UTC)
    async with database.immediate_session() as writer:
        receipt = await writer.get(MemoryToolReceiptModel, batch.first_receipt_id)
        template = {
            column.name: getattr(receipt, column.name) for column in receipt.__table__.columns
        }
        template.pop("id")
        receipt_ids = list(
            await writer.scalars(
                insert(MemoryToolReceiptModel).returning(MemoryToolReceiptModel.id),
                [
                    dict(template, source_call_key=f"{i + 1:064x}", tool_call_id=f"unreadable-{i}")
                    for i in range(40)
                ],
            )
        )
        await writer.execute(
            insert(MemoryEvidenceModel),
            [
                dict(
                    fact_id=fact_id,
                    tool_receipt_id=receipt_id,
                    relation="agent_reflection",
                    authority="agent_reflection",
                    confidence=0.5,
                    source_speaker_user_id="8000",
                    excerpt="没有出现在原执行回执的内容",
                    created_at=now,
                )
                for receipt_id in receipt_ids
            ],
        )
    candidates = await read_self_initiative_facts(
        database, canonical_conversation_id=source.canonical_conversation_id, fact_ids=(fact_id,)
    )
    assert [item.id for item in candidates] == [fact_id]
    assert (await facts.get_fact(fact_id)).evidence_count == 1


async def test_exact_saved_sources_are_not_capped_by_candidate_page_limits(database):
    from sqlalchemy import insert

    _facts, source, _run, _batch, fact_id = await reflection_fact(database)
    async with database.immediate_session() as writer:
        fact = await writer.get(MemoryFactModel, fact_id)
        template = {column.name: getattr(fact, column.name) for column in fact.__table__.columns}
        template.pop("id")
        evidence = await writer.scalar(
            select(MemoryEvidenceModel).where(MemoryEvidenceModel.fact_id == fact_id)
        )
        evidence_template = {
            column.name: getattr(evidence, column.name) for column in evidence.__table__.columns
        }
        evidence_template.pop("id")
        ids = list(
            await writer.scalars(
                insert(MemoryFactModel).returning(MemoryFactModel.id),
                [dict(template, memory_key=f"self:requested-page:{i}") for i in range(139)],
            )
        )
        await writer.execute(
            insert(MemoryEvidenceModel),
            [dict(evidence_template, fact_id=identity) for identity in ids],
        )
    expected = {fact_id, *ids}
    arguments = dict(canonical_conversation_id=source.canonical_conversation_id)
    selected = await read_self_initiative_facts(database, fact_ids=tuple(expected), **arguments)
    assert {item.id for item in selected} == expected


@pytest.mark.parametrize(
    "change",
    [
        {"review_state": "legacy_unreviewed"},
        {"review_state": "quarantined"},
        {"status": "superseded"},
        {"valid_from": "future"},
        {"valid_until": "expired"},
        {"visibility_type": "private"},
        {"scope_type": "person"},
    ],
)
async def test_saved_source_requires_current_lifecycle_and_self_visibility(database, change):
    _facts, event, _run, _batch, fact_id = await reflection_fact(database)
    arguments = dict(canonical_conversation_id=event.canonical_conversation_id, fact_ids=(fact_id,))
    assert [fact.id for fact in await read_self_initiative_facts(database, **arguments)] == [
        fact_id
    ]
    change = dict(change)
    now = datetime.now(UTC)
    if change.get("valid_from") == "future":
        change["valid_from"] = now + timedelta(days=1)
    if change.get("valid_until") == "expired":
        change["valid_until"] = now - timedelta(seconds=1)
    if change.get("visibility_type") == "private":
        change.update(
            canonical_visibility_person_id=event.author_person_id,
            canonical_visibility_space_id=None,
        )
    if change.get("scope_type") == "person":
        change.update(
            canonical_subject_person_id=event.author_person_id,
            visibility_type=None,
            canonical_visibility_space_id=None,
            authority="self_report",
        )
    async with database.immediate_session() as writer:
        await writer.execute(
            update(MemoryFactModel).where(MemoryFactModel.id == fact_id).values(**change)
        )
    assert await read_self_initiative_facts(database, **arguments) == ()


async def test_saved_source_requires_a_current_readable_evidence(database):
    _facts, event, _run, _batch, fact_id = await reflection_fact(database)
    async with database.immediate_session() as writer:
        await writer.execute(
            update(MemoryEvidenceModel)
            .where(MemoryEvidenceModel.fact_id == fact_id)
            .values(excerpt="不是原工具回执的内容")
        )
    assert (
        await read_self_initiative_facts(
            database, canonical_conversation_id=event.canonical_conversation_id, fact_ids=(fact_id,)
        )
        == ()
    )


async def test_exact_reader_does_not_enumerate_unrequested_facts(database):
    _facts, event, _run, _batch, fact_id = await reflection_fact(database)
    arguments = dict(canonical_conversation_id=event.canonical_conversation_id)
    assert await read_self_initiative_facts(database, fact_ids=(), **arguments) == ()
    assert await read_self_initiative_facts(database, fact_ids=(fact_id + 1,), **arguments) == ()
    assert (
        await read_self_initiative_facts(
            database, canonical_conversation_id="missing", fact_ids=(fact_id,)
        )
        == ()
    )
    async with database.sessions() as session:
        selected = await read_self_initiative_facts(
            database, fact_ids=(fact_id,), session=session, **arguments
        )
        assert [fact.id for fact in selected] == [fact_id]
