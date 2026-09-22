"""Evidence preparation must not hold a SQLite writer or duplicate a claim."""

import asyncio
import importlib
import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event, text, update
from tests.conftest import make_settings
from tests.unit.test_relationships import (
    CapturingRelationshipProvider,
    _add_canonical_person_with_aliases,
    append_user_event,
)

from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel
from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.memory.repository import MemoryJobRepository
from qq_ai_bot.persistence.models import ChatEventModel, MemoryJobModel, RelationshipJobModel
from qq_ai_bot.persistence.relationship_repository import RelationshipJobRepository
from qq_ai_bot.persistence.repositories import EventLedgerRepository
from qq_ai_bot.services.concurrency import ConcurrencyManager
from qq_ai_bot.services.relationship_evaluator import LLMRelationshipEvaluator


@pytest.mark.parametrize("kind", ["relationship", "memory", "memory_batch"])
@pytest.mark.parametrize("stale_processing", [False, True])
async def test_claim_prepares_without_writer_and_competing_claims_are_disjoint(
    database, monkeypatch, kind, stale_processing
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

    if stale_processing:
        model = RelationshipJobModel if kind == "relationship" else MemoryJobModel
        async with database.sessions() as session, session.begin():
            await session.execute(
                update(model).values(
                    status="processing", updated_at=datetime.now(UTC) - timedelta(minutes=6)
                )
            )

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


@pytest.mark.parametrize("kind", ["memory", "memory_batch"])
@pytest.mark.parametrize("watermark", ["last_generation_change_event_id", "starts_after_event_id"])
async def test_memory_claim_rechecks_reset_between_prepare_and_commit(
    database, monkeypatch, kind, watermark
):
    identity = await append_user_event(database, message_id="reset-claim")
    repository = MemoryJobRepository(database)
    assert await repository.enqueue(identity, "private:1001")
    module = importlib.import_module("qq_ai_bot.memory.repository")
    original = module.commit_job_claims

    async def reset_then_commit(*args):
        async with database.sessions() as session, session.begin():
            source = await session.get(ChatEventModel, identity)
            values = {watermark: identity}
            if watermark == "starts_after_event_id":
                values["covered_through_event_id"] = identity
            await session.execute(
                update(CanonicalConversationModel)
                .where(CanonicalConversationModel.id == source.canonical_conversation_id)
                .values(**values)
            )
        return await original(*args)

    monkeypatch.setattr(module, "commit_job_claims", reset_then_commit)
    claimed = (
        await repository.claim()
        if kind == "memory"
        else await repository.claim_ready_batch(
            limit=10, trigger_count=1, max_characters=8000, max_wait_seconds=3600
        )
    )
    assert not claimed
    async with database.sessions() as session:
        job = await session.get(MemoryJobModel, 1)
        assert job is not None and job.status == "pending"


async def test_relationship_evidence_uses_all_person_bindings_in_only_the_trigger_conversation(
    database,
):
    first, second = "6610001", "6610002"
    await _add_canonical_person_with_aliases(database, first, second)
    wanted = [await append_user_event(database, message_id="first-binding", user_id=first)]
    hidden = await append_user_event(database, message_id="hidden-evidence", user_id=first)
    async with database.sessions() as session, session.begin():
        await session.execute(
            update(ChatEventModel)
            .where(ChatEventModel.id == hidden)
            .values(suppression_status="duplicate", utterance_fingerprint="d" * 64)
        )
    await append_user_event(database, message_id="other-person", user_id="1001")
    await EventLedgerRepository(database).append(
        bot_user_id="8000",
        platform_message_id="other-conversation",
        scope_type=ScopeType.GROUP,
        group_id="2001",
        sender_user_id=first,
        direction="inbound",
        content="same person in a different conversation",
    )
    wanted.append(await append_user_event(database, message_id="second-binding", user_id=second))
    repository = RelationshipJobRepository(database)
    await repository.enqueue(
        trigger_event_id=wanted[-1], user_id=second, conversation_key=f"private:{second}"
    )
    claimed = await repository.claim()
    assert len(claimed) == 1
    assert claimed[0].user_id == first  # This display projection must not filter the evidence.
    assert [event.id for event in claimed[0].recent_events] == wanted
    provider = CapturingRelationshipProvider(claimed[0].job_id)
    evaluator = LLMRelationshipEvaluator(
        settings=make_settings(database.url),
        provider=provider,
        concurrency=ConcurrencyManager(1),
    )
    await evaluator.evaluate(claimed)
    assert provider.request is not None
    payload = json.loads(provider.request.messages[-1].content or "[]")
    assert [event["event_id"] for event in payload[0]["events"]] == wanted
    assert {event["sender_user_id"] for event in payload[0]["events"]} == {first, second}


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
        migration.upgrade()  # Existing metadata-created or manually indexed databases also upgrade.
        assert inspect(connection).get_indexes("chat_events")[0]["column_names"] == [
            "canonical_conversation_id",
            "author_person_id",
            "id",
        ]
        migration.downgrade()
        assert not inspect(connection).get_indexes("chat_events")
    engine.dispose()
