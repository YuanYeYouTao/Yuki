"""Actual ORM candidate SQL keeps receipts/count semantics and uses a covering search."""

import asyncio
import json
import sqlite3
from datetime import UTC, datetime
from time import perf_counter

import pytest
from sqlalchemy import delete, event, insert, select, update
from sqlalchemy.exc import IntegrityError
from tests.unit.test_evidence_compaction_preparation import _seed
from tests.unit.test_memory_v2 import _append_event

from qq_ai_bot.memory.dream.db_models import (
    MemoryEvidenceCompactionItemModel,
    MemoryEvidenceCompactionRunModel,
)
from qq_ai_bot.persistence.models import (
    MemoryEvidenceModel,
    MemoryFactModel,
    MemorySelfReflectionResultModel,
)
from qq_ai_bot.persistence.repositories import EventLedgerRepository

INDEX = "ix_evidence_compaction_items_fact_before_status"


async def _candidate_fixture(database, *, history_items=0):
    _, service, first, sources, reflection = await _seed(database)
    now = datetime.now(UTC)
    await service._backfill_reflection_results()
    async with database.sessions() as session, session.begin():
        template = await session.get(MemoryFactModel, first.id)
        values = {
            column.name: getattr(template, column.name)
            for column in template.__table__.columns
            if column.name != "id"
        }
        fact_ids = [first.id]
        for number in range(1, 7):
            fact_id = await session.scalar(
                insert(MemoryFactModel)
                .values(**{**values, "memory_key": f"candidate-{number}"})
                .returning(MemoryFactModel.id)
            )
            fact_ids.append(fact_id)
            session.add(
                MemorySelfReflectionResultModel(
                    run_id=reflection.id,
                    fact_id=fact_id,
                    result_kind="episode",
                    result_index=number + 1,
                    created_at=now,
                )
            )
            session.add_all(
                [
                    MemoryEvidenceModel(
                        fact_id=fact_id,
                        event_id=source.id,
                        source_speaker_user_id="1001",
                        relation="agent_reflection",
                        confidence=0.05,
                        authority="agent_reflection",
                        excerpt="synthetic evidence",
                        created_at=now,
                    )
                    for source in sources
                ]
            )
        run = MemoryEvidenceCompactionRunModel(
            public_id="retained-original-run", status="running", created_at=now, updated_at=now
        )
        session.add(run)
        await session.flush()
        for number, status in enumerate(
            ("completed", "skipped", "failed", "pending", "processing", "failed")
        ):
            session.add(
                MemoryEvidenceCompactionItemModel(
                    run_id=run.id,
                    fact_id=fact_ids[number],
                    provenance_type="self_reflection",
                    status=status,
                    evidence_before=12 if number == 5 else 13,
                    created_at=now,
                    updated_at=now,
                )
            )
        # Historical terminal rows have a different exact evidence_count and
        # cannot suppress the current candidate. Different run IDs keep the
        # existing (run_id,fact_id) uniqueness realistic and enforced.
        if history_items:
            run_ids = list(
                await session.scalars(
                    insert(MemoryEvidenceCompactionRunModel).returning(
                        MemoryEvidenceCompactionRunModel.id
                    ),
                    [
                        dict(
                            public_id=f"history-{number}",
                            status="completed",
                            created_at=now,
                            updated_at=now,
                        )
                        for number in range(history_items)
                    ],
                )
            )
            await session.execute(
                insert(MemoryEvidenceCompactionItemModel),
                [
                    dict(
                        run_id=run_id,
                        fact_id=first.id,
                        provenance_type="self_reflection",
                        status="completed",
                        evidence_before=1,
                        created_at=now,
                        updated_at=now,
                    )
                    for run_id in run_ids
                ],
            )
    return service, fact_ids, sources, run.id


async def _actual_candidate_sql(database, service):
    captured = []

    def observe(_connection, _cursor, statement, parameters, *_args):
        if (
            statement.lstrip().startswith("SELECT")
            and "FROM memory_evidence_compaction_items" in statement
        ):
            captured.append((statement, parameters))

    event.listen(database.engine.sync_engine, "before_cursor_execute", observe)
    try:
        result = await service._candidate_facts(limit=100)
    finally:
        event.remove(database.engine.sync_engine, "before_cursor_execute", observe)
    assert len(captured) == 1
    return result, captured[0]


@pytest.mark.parametrize("item_count", [187, 2000, 20000])
async def test_full_orm_candidate_plan_results_and_vm_work(database, item_count):
    service, fact_ids, _, _ = await _candidate_fixture(database, history_items=item_count - 6)
    actual, (statement, parameters) = await _actual_candidate_sql(database, service)
    assert [row[0] for row in actual] == fact_ids[3:]
    path = database.url.removeprefix("sqlite+aiosqlite:///")

    def measure():
        with sqlite3.connect(path) as db:
            before_facts = db.execute("SELECT * FROM memory_facts ORDER BY id").fetchall()
            before_receipts = db.execute(
                "SELECT * FROM memory_evidence_compaction_items ORDER BY id"
            ).fetchall()
            db.execute(f'DROP INDEX "{INDEX}"')
            measurements = []
            unindexed_rows = None
            for indexed in (False, True):
                if indexed:
                    db.execute(
                        f'CREATE INDEX "{INDEX}" ON memory_evidence_compaction_items'
                        "(fact_id,evidence_before,status)"
                    )
                plan = db.execute("EXPLAIN QUERY PLAN " + statement, parameters).fetchall()
                detail = [row[3] for row in plan if "memory_evidence_compaction_items" in row[3]]
                if indexed:
                    assert any(
                        "SEARCH" in row and "COVERING INDEX " + INDEX in row for row in detail
                    )
                else:
                    assert any("SCAN memory_evidence_compaction_items" in row for row in detail)
                vm = 0

                def progress():
                    nonlocal vm
                    vm += 1
                    return 0

                db.set_progress_handler(progress, 1)
                rows = db.execute(statement, parameters).fetchall()
                db.set_progress_handler(None, 0)
                if indexed:
                    assert rows == unindexed_rows
                else:
                    unindexed_rows = rows
                times = []
                for _ in range(5):
                    started = perf_counter()
                    assert db.execute(statement, parameters).fetchall() == rows
                    times.append((perf_counter() - started) * 1000)
                measurements.append(
                    dict(
                        indexed=indexed,
                        vm_steps=vm,
                        samples_ms=times,
                        correlated_plan=detail,
                        candidate_ids=[row[0] for row in rows],
                    )
                )
            assert (
                measurements[0]["candidate_ids"] == measurements[1]["candidate_ids"] == fact_ids[3:]
            )
            assert measurements[1]["vm_steps"] < measurements[0]["vm_steps"]
            assert db.execute("SELECT * FROM memory_facts ORDER BY id").fetchall() == before_facts
            assert (
                db.execute("SELECT * FROM memory_evidence_compaction_items ORDER BY id").fetchall()
                == before_receipts
            )
            db.commit()
            return measurements

    measurements = await asyncio.to_thread(measure)
    assert await service._candidate_facts(limit=100) == actual
    assert await service._candidate_facts(limit=2) == actual[:2]
    print(
        json.dumps(
            dict(
                historical_items=item_count - 6,
                semantic_items=6,
                total_items=item_count,
                fixture="seven exact reflection facts, thirteen evidence each; synthetic only",
                measurements=measurements,
            )
        )
    )


async def test_terminal_counts_pending_and_processing_keep_exact_candidate_semantics(database):
    service, fact_ids, _, run_id = await _candidate_fixture(database)
    assert [row[0] for row in await service._candidate_facts(limit=100)] == fact_ids[3:]
    extra = await _append_event(
        EventLedgerRepository(database), message_id="changed-count", content="synthetic evidence"
    )
    async with database.immediate_session() as session:
        session.add(
            MemoryEvidenceModel(
                fact_id=fact_ids[0],
                event_id=extra.id,
                source_speaker_user_id="1001",
                relation="agent_reflection",
                confidence=0.05,
                authority="agent_reflection",
                excerpt="synthetic",
                created_at=datetime.now(UTC),
            )
        )
    candidates = await service._candidate_facts(limit=100)
    assert candidates[0] == (fact_ids[0], "self_reflection", None, 14)
    async with database.immediate_session() as session:
        await session.execute(
            update(MemoryEvidenceCompactionItemModel)
            .where(MemoryEvidenceCompactionItemModel.run_id == run_id)
            .values(status="completed", evidence_before=13)
        )
        await session.execute(
            update(MemoryEvidenceCompactionItemModel)
            .where(MemoryEvidenceCompactionItemModel.fact_id == fact_ids[0])
            .values(evidence_before=14)
        )
        session.add(
            MemoryEvidenceCompactionItemModel(
                run_id=run_id,
                fact_id=fact_ids[-1],
                provenance_type="self_reflection",
                status="skipped",
                evidence_before=13,
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
        )
    assert await service._candidate_facts(limit=100) == ()
    # Both deleting a fact and deleting its original run preserve FK cascades.
    async with database.immediate_session() as session:
        await session.execute(delete(MemoryFactModel).where(MemoryFactModel.id == fact_ids[0]))
    async with database.sessions() as session:
        assert (
            await session.scalar(
                select(MemoryEvidenceCompactionItemModel.id).where(
                    MemoryEvidenceCompactionItemModel.fact_id == fact_ids[0]
                )
            )
            is None
        )
        assert (
            await session.scalar(
                select(MemoryEvidenceModel.id).where(MemoryEvidenceModel.fact_id == fact_ids[0])
            )
            is None
        )
    with pytest.raises(IntegrityError):
        async with database.immediate_session() as session:
            session.add(
                MemoryEvidenceCompactionItemModel(
                    run_id=run_id,
                    fact_id=fact_ids[1],
                    provenance_type="self_reflection",
                    status="pending",
                    evidence_before=99,
                    created_at=datetime.now(UTC),
                    updated_at=datetime.now(UTC),
                )
            )
    async with database.immediate_session() as session:
        await session.execute(
            delete(MemoryEvidenceCompactionRunModel).where(
                MemoryEvidenceCompactionRunModel.id == run_id
            )
        )
    async with database.sessions() as session:
        assert list(await session.scalars(select(MemoryEvidenceCompactionItemModel))) == []
