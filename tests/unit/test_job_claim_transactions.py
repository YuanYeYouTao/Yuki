"""Evidence preparation must not hold a SQLite writer or duplicate a claim."""

import asyncio
import importlib

import pytest
from sqlalchemy import event, text, update
from tests.unit.test_relationships import append_user_event

from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel
from qq_ai_bot.memory.repository import MemoryJobRepository
from qq_ai_bot.persistence.models import ChatEventModel
from qq_ai_bot.persistence.relationship_repository import RelationshipJobRepository


@pytest.mark.parametrize("kind", ["relationship", "memory", "memory_batch"])
async def test_claim_prepares_without_writer_and_competing_claims_are_disjoint(
    database, monkeypatch, kind
):
    ids = [await append_user_event(database, message_id=f"claim-{i}") for i in range(3)]
    if kind == "relationship":
        repository = RelationshipJobRepository(database)
        for identity in ids:
            await repository.enqueue(
                trigger_event_id=identity, user_id="1001", conversation_key="private:1001"
            )
        module = importlib.import_module("qq_ai_bot.persistence.relationship_repository")
    else:
        repository = MemoryJobRepository(database)
        for identity in ids:
            assert await repository.enqueue(identity, "private:1001")
        module = importlib.import_module("qq_ai_bot.memory.repository")

    writers = set()

    def observe(conn, cursor, sql, params, context, many):
        normalized = sql.lstrip().upper()
        if normalized.startswith(("UPDATE", "INSERT", "DELETE")):
            writers.add(id(conn))
        if normalized.startswith("SELECT") and "CHAT_EVENTS" in normalized:
            assert id(conn) not in writers, "evidence SELECT after acquiring the writer"

    def clear(conn):
        writers.discard(id(conn))

    engine = database.engine.sync_engine
    event.listen(engine, "before_cursor_execute", observe)
    event.listen(engine, "commit", clear)
    event.listen(engine, "rollback", clear)
    original = module.commit_job_claims
    ready = asyncio.Event()
    arrivals = 0

    async def overlap(*args):
        nonlocal arrivals
        arrivals += 1
        if arrivals == 2:
            ready.set()
        await asyncio.wait_for(ready.wait(), 3)
        return await original(*args)

    monkeypatch.setattr(module, "commit_job_claims", overlap)

    async def claim():
        if kind == "memory_batch":
            return await repository.claim_ready_batch(
                limit=10, trigger_count=1, max_characters=8000, max_wait_seconds=3600
            )
        return await repository.claim(limit=10)

    try:
        first, second = await asyncio.gather(claim(), claim())
    finally:
        event.remove(engine, "before_cursor_execute", observe)
        event.remove(engine, "commit", clear)
        event.remove(engine, "rollback", clear)
    claimed = [getattr(job, "job_id", None) or job.id for job in (*first, *second)]
    assert len(claimed) == len(set(claimed)) == 3


async def test_memory_claim_rechecks_reset_between_prepare_and_commit(database, monkeypatch):
    identity = await append_user_event(database, message_id="reset-claim")
    repository = MemoryJobRepository(database)
    assert await repository.enqueue(identity, "private:1001")
    module = importlib.import_module("qq_ai_bot.memory.repository")
    original = module.commit_job_claims

    async def reset_then_commit(*args):
        async with database.sessions() as session, session.begin():
            source = await session.get(ChatEventModel, identity)
            await session.execute(
                update(CanonicalConversationModel)
                .where(CanonicalConversationModel.id == source.canonical_conversation_id)
                .values(last_generation_change_event_id=identity)
            )
        return await original(*args)

    monkeypatch.setattr(module, "commit_job_claims", reset_then_commit)
    assert not await repository.claim()


async def test_relationship_index_matches_canonical_ordered_lookup(database):
    async with database.sessions() as session:
        plan = await session.execute(
            text(
                "EXPLAIN QUERY PLAN SELECT id FROM chat_events "
                "WHERE canonical_conversation_id = 'c' AND author_person_id = 'p' "
                "AND id <= 100 ORDER BY id DESC LIMIT 5"
            )
        )
        detail = " ".join(str(row) for row in plan)
    assert "ix_chat_events_conversation_author_id" in detail
    assert "TEMP B-TREE" not in detail


def test_relationship_index_migration_round_trip(monkeypatch):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import create_engine, inspect

    migration = importlib.import_module("migrations.versions.0065_relationship_history_index")
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE chat_events (id INTEGER PRIMARY KEY, "
                "canonical_conversation_id TEXT, author_person_id TEXT)"
            )
        )
        monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
        migration.upgrade()
        assert inspect(connection).get_indexes("chat_events")[0]["column_names"] == [
            "canonical_conversation_id",
            "author_person_id",
            "id",
        ]
        migration.downgrade()
        assert not inspect(connection).get_indexes("chat_events")
    engine.dispose()
