"""Canonical evidence preserves authorized sources, order, and requested limits."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import insert, select
from tests.support.self_initiative_memory_quality_helpers import reflection_fact
from tests.unit.test_memory_v2 import _append_event

from qq_ai_bot.memory.models import MemoryEvidenceCreate, MemoryFactCreate
from qq_ai_bot.memory.repository import MemoryFactRepository
from qq_ai_bot.memory.service import MemoryFactService
from qq_ai_bot.persistence.models import (
    ChatEventModel,
    MemoryEvidenceModel,
    MemoryToolReceiptModel,
)
from qq_ai_bot.persistence.repositories import EventLedgerRepository


@pytest.mark.parametrize("size", [1, 64, 1000])
async def test_evidence_order_limit_and_counts_are_preserved(database, size):
    repository = MemoryFactRepository(database)
    facts = MemoryFactService(repository)
    source = await _append_event(EventLedgerRepository(database), message_id="seed", content="seed")
    fact = await facts.remember(
        MemoryFactCreate(
            scope_type="person",
            subject_user_id="1001",
            memory_key="batch:scale",
            category="test",
            content="readable evidence",
            source_type="automatic",
        )
    )
    async with database.sessions() as session:
        row = await session.get(ChatEventModel, source.id)
        template = {column.name: getattr(row, column.name) for column in row.__table__.columns}
    template.pop("id")
    now = datetime.now(UTC)
    async with database.sessions() as session, session.begin():
        event_ids = list(
            (
                await session.scalars(
                    insert(ChatEventModel).returning(ChatEventModel.id),
                    [
                        dict(
                            template,
                            platform_message_id=f"scale-{i}",
                            canonical_event_id=str(uuid4()),
                        )
                        for i in range(size)
                    ],
                )
            ).all()
        )
        await session.execute(
            insert(MemoryEvidenceModel),
            [
                dict(
                    fact_id=fact.id,
                    event_id=event_id,
                    source_speaker_user_id="1001",
                    relation="confirmation",
                    confidence=0.3,
                    authority="self_report",
                    excerpt=f"item-{i}",
                    created_at=now,
                )
                for i, event_id in enumerate(event_ids)
            ],
        )
    evidence = await repository.list_evidence(fact.id, limit=size)
    assert len(evidence) == size
    assert [row.excerpt for row in evidence] == [f"item-{i}" for i in reversed(range(size))]
    assert [row.id for row in await repository.list_evidence(fact.id, limit=1)] == [evidence[0].id]
    assert (await facts.get_fact(fact.id)).evidence_count == size


async def test_all_owner_shapes_only_expose_authorized_chat_and_tool_sources(database):
    ledger = EventLedgerRepository(database)
    sources = [
        await _append_event(
            ledger, message_id=f"source-{i}", content=f"source-{i}", user_id=user, group_id=group
        )
        for i, (user, group) in enumerate(
            [
                ("1001", None),
                ("1002", None),
                ("1001", "2001"),
                ("1001", "2002"),
                ("1001", None),
                ("1001", None),
            ]
        )
    ]
    facts = MemoryFactService(MemoryFactRepository(database))
    targets = (
        dict(scope_type="person", subject_user_id="1001"),
        dict(scope_type="group", group_id="2001"),
        dict(scope_type="person_group", subject_user_id="1001", group_id="2001"),
        dict(scope_type="self", visibility_type="global"),
        dict(scope_type="self", visibility_type="private", visibility_user_id="1001"),
        dict(scope_type="self", visibility_type="group", visibility_group_id="2001"),
    )
    saved = [
        await facts.remember(
            MemoryFactCreate(
                **target,
                memory_key=f"batch:owner-{i}",
                category="test",
                content="source-chain compatibility",
                source_type="automatic",
            )
        )
        for i, target in enumerate(targets)
    ]
    now = datetime.now(UTC)
    async with database.sessions() as session, session.begin():
        hidden = await session.get(ChatEventModel, sources[4].id)
        hidden.utterance_fingerprint = "a" * 64
        hidden.suppression_status = "duplicate"
        moved = await session.get(ChatEventModel, sources[5].id)
        moved.canonical_conversation_id = sources[1].canonical_conversation_id
        receipts = [
            MemoryToolReceiptModel(
                conversation_key_hash=f"source-{i}",
                trigger_event_id=source.id,
                bot_user_id="8000",
                canonical_person_id=source.author_person_id,
                canonical_space_id=None,
                provider_id="fake",
                tool_name="test",
                success=True,
                result_excerpt="recorded",
                result_characters=8,
                created_at=now,
                expires_at=now + timedelta(days=30),
            )
            for i, source in enumerate(sources)
        ]
        session.add_all(receipts)
        await session.flush()
        session.add_all(
            [
                MemoryEvidenceModel(
                    fact_id=fact.id,
                    event_id=source.id if kind == "event" else None,
                    tool_receipt_id=receipt.id if kind == "tool" else None,
                    source_speaker_user_id="1001",
                    relation="confirmation",
                    confidence=0.4,
                    authority="self_report",
                    excerpt=f"{kind}-{source.id}",
                    created_at=now,
                )
                for fact in saved
                for source, receipt in zip(sources, receipts, strict=True)
                for kind in ("event", "tool")
            ]
        )
    allowed_sources = ((0, 2, 3), (2,), (2,), (0, 1, 2, 3, 5), (0,), (2,))
    for fact, allowed in zip(saved, allowed_sources, strict=True):
        async with database.sessions() as session:
            candidates = list(
                await session.scalars(
                    select(MemoryEvidenceModel)
                    .where(MemoryEvidenceModel.fact_id == fact.id)
                    .order_by(MemoryEvidenceModel.created_at.desc(), MemoryEvidenceModel.id.desc())
                )
            )
            event_ids = {sources[i].id for i in allowed}
            receipt_ids = {receipts[i].id for i in allowed}
            expected = [
                row.id
                for row in candidates
                if row.event_id in event_ids or row.tool_receipt_id in receipt_ids
            ]
        actual = await facts.list_evidence(fact.id)
        assert [row.id for row in actual] == expected
        assert (await facts.get_fact(fact.id)).evidence_count == len(expected)
    # The visibility filter is applied before LIMIT: newer hidden rows cannot
    # consume the requested readable evidence budget.
    assert len(await facts.list_evidence(saved[0].id, limit=3)) == 3
    before = await facts.list_evidence(saved[0].id)
    new_source = await _append_event(
        ledger, message_id="new-confirmation", content="new confirmation"
    )
    addition = MemoryEvidenceCreate(
        event_id=new_source.id,
        source_speaker_user_id="1001",
        relation="confirmation",
        authority="self_report",
        confidence=0.2,
        excerpt="new confirmation",
    )
    confirmed = await facts.confirm_fact(saved[0].id, addition)
    assert confirmed.confidence == saved[0].confidence
    # Visibility is evaluated from current durable rows on every call. Source
    # cascade removal cannot leave a cached evidence or count projection alive.
    async with database.sessions() as session, session.begin():
        erased = await session.get(ChatEventModel, sources[0].id)
        await session.delete(erased)
    remaining = await facts.list_evidence(saved[0].id)
    assert len(remaining) == len(before) - 1  # Two old source rows removed, one new row added.
    assert not any(row.event_id == erased.id for row in remaining)
    assert (await facts.get_fact(saved[0].id)).evidence_count == len(remaining)


async def test_self_initiative_sources_share_sql_readability_and_do_not_borrow_events(database):
    facts, _source, _run_id, batch, fact_id = await reflection_fact(database)
    async with database.sessions() as session:
        candidates = list(
            await session.scalars(
                select(MemoryEvidenceModel).where(MemoryEvidenceModel.fact_id == fact_id)
            )
        )
        expected = [row.id for row in candidates]
    assert [row.id for row in await facts.list_evidence(fact_id)] == expected
    async with database.sessions() as session, session.begin():
        receipt = await session.get(MemoryToolReceiptModel, batch.first_receipt_id)
        receipt.result_excerpt = "source was erased"
    assert await facts.list_evidence(fact_id) == ()
    assert (await facts.get_fact(fact_id)).evidence_count == 0


async def test_initiative_receipt_cannot_borrow_a_chat_event_source(database):
    facts, source, _run, batch, fact_id = await reflection_fact(database)
    async with database.immediate_session() as writer:
        receipt = await writer.get(MemoryToolReceiptModel, batch.last_receipt_id)
        receipt.trigger_event_id = source.id
        await writer.flush()
        before = await facts.repository.list_evidence(fact_id, session=writer)
        assert not await facts.repository.add_evidence(
            fact_id,
            MemoryEvidenceCreate(
                tool_receipt_id=receipt.id,
                source_speaker_user_id="8000",
                relation="agent_reflection",
                authority="agent_reflection",
                excerpt="已生成并校验曲线绘图",
            ),
            session=writer,
        )
        assert await facts.repository.list_evidence(fact_id, session=writer) == before
