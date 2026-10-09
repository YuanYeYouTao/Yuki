"""Canonical evidence is filtered in one SQL read, including hidden source chains."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import event, insert, select
from tests.support.self_initiative_memory_quality_helpers import reflection_fact
from tests.unit.test_memory_v2 import _append_event

from qq_ai_bot.identity.memory_guard import v2_evidence_row_readable
from qq_ai_bot.memory.models import MemoryEvidenceCreate, MemoryFactCreate
from qq_ai_bot.memory.repository import MemoryFactRepository
from qq_ai_bot.memory.service import MemoryFactService
from qq_ai_bot.persistence.models import (
    ChatEventModel,
    MemoryEvidenceModel,
    MemoryFactModel,
    MemoryToolReceiptModel,
)
from qq_ai_bot.persistence.repositories import EventLedgerRepository


@pytest.mark.parametrize("size", [1, 64, 1000])
async def test_evidence_query_round_trips_are_constant_and_order_limit_is_preserved(database, size):
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
    statements = []

    def count(_connection, _cursor, statement, *_args):
        statements.append(statement)

    event.listen(database.engine.sync_engine, "before_cursor_execute", count)
    try:
        evidence = await repository.list_evidence(fact.id, limit=size)
    finally:
        event.remove(database.engine.sync_engine, "before_cursor_execute", count)
    assert len(statements) == 1
    assert len(evidence) == size
    assert [row.excerpt for row in evidence] == [f"item-{i}" for i in reversed(range(size))]
    assert [row.id for row in await repository.list_evidence(fact.id, limit=1)] == [evidence[0].id]
    assert (await facts.get_fact(fact.id)).evidence_count == size


async def test_all_owner_shapes_and_chat_tool_sources_match_reference_guards(database):
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
    for fact in saved:
        async with database.sessions() as session:
            owner = await session.get(MemoryFactModel, fact.id)
            candidates = list(
                await session.scalars(
                    select(MemoryEvidenceModel)
                    .where(MemoryEvidenceModel.fact_id == fact.id)
                    .order_by(MemoryEvidenceModel.created_at.desc(), MemoryEvidenceModel.id.desc())
                )
            )
            expected = [
                row.id for row in candidates if await v2_evidence_row_readable(session, owner, row)
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
        owner = await session.get(MemoryFactModel, fact_id)
        candidates = list(
            await session.scalars(
                select(MemoryEvidenceModel).where(MemoryEvidenceModel.fact_id == fact_id)
            )
        )
        expected = [
            row.id for row in candidates if await v2_evidence_row_readable(session, owner, row)
        ]
    assert [row.id for row in await facts.list_evidence(fact_id)] == expected
    async with database.sessions() as session, session.begin():
        receipt = await session.get(MemoryToolReceiptModel, batch.first_receipt_id)
        receipt.result_excerpt = "source was erased"
    assert await facts.list_evidence(fact_id) == ()
    assert (await facts.get_fact(fact_id)).evidence_count == 0
