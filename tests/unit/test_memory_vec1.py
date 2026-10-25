"""Native vector ownership, authorization, migration and original score semantics."""

import asyncio
import importlib.util
import math
import random
import sqlite3
import struct
import threading
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import aiosqlite
import pytest
from sqlalchemy import create_engine, event, select, text
from tests.support.canonical_identity import ensure_person
from tests.support.user_profiles_helpers import observe_members
from tests.unit.test_embedding_claim_boundaries import _jobs
from tests.unit.test_memory_partition import _owners
from tests.unit.test_memory_retrieval import _query

from yuki.memory.authorized_scope import AuthorizedMemoryScope
from yuki.memory.embedding.codec import Float32VectorCodec
from yuki.memory.embedding.fake import FakeEmbeddingProvider
from yuki.memory.embedding.models import EmbeddingVector
from yuki.memory.embedding.repository import MemoryEmbeddingRepository
from yuki.memory.embedding.semantic import MemorySemanticIndex
from yuki.memory.embedding.text import EmbeddingQueryBuilder
from yuki.memory.enums import MemoryScopeType
from yuki.memory.fts import SQLiteMemoryFTSIndex
from yuki.memory.quality.audit import MemoryProductionQualityAudit
from yuki.memory.quality.hygiene import MemoryProvenanceHygiene
from yuki.memory.repository import MemoryFactRepository
from yuki.memory.retrieval import MemoryRetriever
from yuki.persistence.models import MembershipModel, MemoryEmbeddingModel, MemoryFactModel
from yuki.persistence.vec1 import extension_path, vector_table


async def populate(session, jobs, facts, values):
    codec = Float32VectorCodec()
    now = datetime.now(UTC)
    await MemoryEmbeddingRepository.store_vectors(
        session,
        profile_id=jobs.profile.id,
        records=[
            dict(
                fact_id=fact.id,
                profile_id=jobs.profile.id,
                content_hash=jobs.documents.content_hash_fields(
                    kind=fact.kind,
                    category=fact.category,
                    memory_key=fact.memory_key,
                    content=fact.content,
                ),
                vector_blob=codec.encode(EmbeddingVector(values=value, dimensions=2)),
                created_at=now,
                updated_at=now,
            )
            for fact, value in zip(facts, values, strict=True)
        ],
    )


async def test_current_authorization_filters_inside_native_top_k(database):
    jobs = await _jobs(database, 2)
    repository = MemoryEmbeddingRepository(database)
    captured = []

    def statements(_connection, _cursor, statement, parameters, *_):
        if "memory_vectors_1(" in statement and statement.startswith("SELECT"):
            captured.append((statement, parameters))

    async with database.immediate_session() as session:
        facts = list(await session.scalars(select(MemoryFactModel).order_by(MemoryFactModel.id)))
        outsider = await ensure_person(session, "unrelated-person")
        facts[0].canonical_subject_person_id = outsider
        await session.flush()
        await populate(session, jobs, facts, [(1.0, 0.0), (0.8, 0.6)])
    query = Float32VectorCodec().encode(EmbeddingVector(values=(1.0, 0.0), dimensions=2))
    vectors = vector_table(jobs.profile.id)
    async with database.read_session() as session:
        global_first = await session.scalar(
            text(f"SELECT fact_id FROM {vectors.name}(:query, :params)"),
            {"query": query, "params": '{"K":1}'},
        )
    assert global_first == facts[0].id
    event.listen(database.engine.sync_engine, "after_cursor_execute", statements)
    try:
        loaded = await repository.load_authorized_vectors(
            scope=AuthorizedMemoryScope(
                requester_person_id=facts[1].canonical_subject_person_id,
                allowed_scopes=(MemoryScopeType.PERSON,),
            ),
            profile_id=jobs.profile.id,
            kinds=(),
            query_blob=query,
            candidate_count=1,
        )
    finally:
        event.remove(database.engine.sync_engine, "after_cursor_execute", statements)
    assert [row.fact_id for row in loaded.rows] == [facts[1].id]
    assert loaded.active_count == loaded.indexed_count == 1
    sql, parameters = captured[-1]
    async with database.read_session() as session:
        connection = await session.connection()
        plan = (await connection.exec_driver_sql("EXPLAIN QUERY PLAN " + sql, parameters)).all()
    assert any("PI" in row[-1] for row in plan)


async def test_native_candidates_follow_current_historical_membership_without_reindex(database):
    await observe_members(database, ("1001", "1002"))
    await observe_members(database, ("1003",), group_id="2002")
    people, spaces = await _owners(database)
    jobs = await _jobs(database, 3)
    async with database.immediate_session() as session:
        facts = list(await session.scalars(select(MemoryFactModel).order_by(MemoryFactModel.id)))
        facts[0].canonical_subject_person_id = people["1002"]
        facts[1].canonical_subject_person_id = people["1003"]
        await session.flush()
        await populate(session, jobs, facts, [(1.0, 0.0), (1.0, 0.0), (0.0, 1.0)])
    repository = MemoryEmbeddingRepository(database)
    arguments = dict(
        scope=AuthorizedMemoryScope(
            requester_person_id=people["1001"], allowed_scopes=(MemoryScopeType.PERSON,)
        ),
        profile_id=jobs.profile.id,
        kinds=(),
        query_blob=struct.pack("<2f", 1.0, 0.0),
        candidate_count=1,
    )
    assert [
        row.fact_id for row in (await repository.load_authorized_vectors(**arguments)).rows
    ] == [facts[0].id]
    async with database.immediate_session() as session:
        await session.execute(
            MembershipModel.__table__.delete().where(
                MembershipModel.canonical_person_id == people["1002"],
                MembershipModel.canonical_space_id == spaces["2001"],
            )
        )
    assert [
        row.fact_id for row in (await repository.load_authorized_vectors(**arguments)).rows
    ] == [facts[2].id]


async def test_fact_cascade_and_vector_replace_are_one_transaction(database):
    jobs = await _jobs(database)
    async with database.immediate_session() as session:
        fact = await session.scalar(select(MemoryFactModel))
        await populate(session, jobs, [fact], [(1.0, 0.0)])
    vectors = vector_table(jobs.profile.id)
    with pytest.raises(RuntimeError, match="rollback"):
        async with database.immediate_session() as session:
            await populate(session, jobs, [fact], [(0.0, 1.0)])
            await session.execute(
                MemoryFactModel.__table__.delete().where(MemoryFactModel.id == fact.id)
            )
            assert await session.scalar(select(vectors.c.rowid)) is None
            raise RuntimeError("rollback")
    async with database.read_session() as session:
        assert await session.scalar(select(vectors.c.vector)) == struct.pack("<2f", 1.0, 0.0)
        assert await session.scalar(select(MemoryEmbeddingModel.fact_id)) == fact.id
    async with database.immediate_session() as session:
        await session.execute(
            MemoryFactModel.__table__.delete().where(MemoryFactModel.id == fact.id)
        )
    async with database.read_session() as session:
        assert await session.scalar(select(vectors.c.rowid)) is None
        assert await session.scalar(select(MemoryEmbeddingModel.id)) is None


async def test_candidate_approximation_is_distinct_from_missing_vectors(database):
    jobs = await _jobs(database, 8)
    async with database.immediate_session() as session:
        facts = list(await session.scalars(select(MemoryFactModel).order_by(MemoryFactModel.id)))
        await populate(session, jobs, facts, [(1.0, 0.0)] * 8)
    index = MemorySemanticIndex(MemoryEmbeddingRepository(database), documents=jobs.documents)
    kwargs = dict(
        scope=AuthorizedMemoryScope(
            requester_person_id=facts[0].canonical_subject_person_id,
            allowed_scopes=(MemoryScopeType.PERSON,),
        ),
        query_vector=EmbeddingVector(values=(1.0, 0.0), dimensions=2),
        profile=jobs.profile.profile,
        profile_id=jobs.profile.id,
        candidate_limit=1,
        kinds=(),
    )
    result = await index.search_authorized(**kwargs)
    assert result.approximate and result.coverage_complete
    retriever = MemoryRetriever(
        repository=MemoryFactRepository(database), lexical_index=SQLiteMemoryFTSIndex(database)
    )
    retriever.configure_semantic(
        semantic_index=index,
        provider=FakeEmbeddingProvider(dimensions=2),
        profile=jobs.profile,
        queries=EmbeddingQueryBuilder(max_characters=1000),
    )
    outcome = await retriever.retrieve_authorized(
        _query("fact").model_copy(update={"semantic_enabled": True, "semantic_candidate_limit": 1}),
        kwargs["scope"],
        limit=20,
    )
    assert outcome.semantic_approximate and not outcome.exhaustive
    assert outcome.semantic_status == "ready" and not outcome.partial_failure
    async with database.immediate_session() as session:
        await session.execute(
            MemoryEmbeddingModel.__table__.delete().where(
                MemoryEmbeddingModel.fact_id == facts[0].id
            )
        )
    result = await index.search_authorized(**kwargs)
    assert result.approximate and not result.coverage_complete


async def test_empty_profile_has_no_candidates_and_reports_missing_index_coverage(database):
    jobs = await _jobs(database)
    loaded = await MemoryEmbeddingRepository(database).load_authorized_vectors(
        scope=AuthorizedMemoryScope(
            requester_person_id="no-person", allowed_scopes=(MemoryScopeType.PERSON,)
        ),
        profile_id=jobs.profile.id,
        kinds=(),
        query_blob=struct.pack("<2f", 1.0, 0.0),
        candidate_count=4,
    )
    assert loaded.rows == () and loaded.active_count == loaded.indexed_count == 0


async def test_missing_raw_value_is_visible_to_audit_hygiene_and_original_job(database):
    jobs = await _jobs(database)
    async with database.immediate_session() as session:
        fact = await session.scalar(select(MemoryFactModel))
        await populate(session, jobs, [fact], [(1.0, 0.0)])
        await session.execute(vector_table(jobs.profile.id).delete())
    report = await MemoryProductionQualityAudit(database).run()
    assert any(
        issue.issue_code == "embedding_dimension_mismatch" and issue.count == 1
        for issue in report.issues
    )
    plan = await MemoryProvenanceHygiene(database).scan()
    assert plan.enqueue_embedding_fact_ids == (fact.id,)
    assert await jobs.reconcile() == 1


@pytest.mark.parametrize("write", [False, True])
async def test_cancel_waits_for_actual_native_driver_exit(database, monkeypatch, write):
    if write:
        jobs = await _jobs(database)
        async with database.immediate_session() as session:
            fact = await session.scalar(select(MemoryFactModel))
            await populate(session, jobs, [fact], [(1.0, 0.0)])
    entered = asyncio.Event()
    finished = threading.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    execute = aiosqlite.Connection._execute

    async def native_operation(connection, function, *args, **kwargs):
        prefix = "INSERT INTO memory_vectors_" if write else "SELECT vec1_cos_distance"
        if args and str(args[0]).startswith(prefix):

            def run():
                loop.call_soon_threadsafe(entered.set)
                assert release.wait(5)
                try:
                    return function(*args, **kwargs)
                finally:
                    finished.set()

            return await execute(connection, run)
        return await execute(connection, function, *args, **kwargs)

    monkeypatch.setattr(aiosqlite.Connection, "_execute", native_operation)

    async def read():
        if write:
            async with database.immediate_session() as session:
                await populate(session, jobs, [fact], [(0.0, 1.0)])
            return None
        async with database.read_session() as session:
            return await session.scalar(
                text("SELECT vec1_cos_distance(:left,:right)"),
                {"left": struct.pack("<2f", 1.0, 0.0), "right": struct.pack("<2f", 0.0, 1.0)},
            )

    task = asyncio.create_task(read())
    try:
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done() and not finished.is_set()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished.is_set()
    async with database.read_session() as session:
        assert await session.scalar(text("SELECT 1")) == 1
        if write:
            assert await session.scalar(
                select(vector_table(jobs.profile.id).c.vector)
            ) == struct.pack("<2f", 1.0, 0.0)


def test_decode_normalized_retains_original_float_steps_and_rejects_bad_norm():
    rng = random.Random(1026)
    codec = Float32VectorCodec()
    for dimensions in (2, 32, 1024):
        for _ in range(12):
            vector = EmbeddingVector(
                values=tuple(rng.uniform(-1, 1) for _ in range(dimensions)), dimensions=dimensions
            )
            raw = codec.encode(vector)
            old = codec.normalize(codec.decode(raw, dimensions=dimensions))
            new = codec.decode_normalized(raw, dimensions=dimensions)
            assert tuple(value.hex() for value in old.values) == tuple(
                value.hex() for value in new.values
            )
            expected = math.fsum(a * b for a, b in zip(old.values, new.values, strict=True))
            assert codec.dot_normalized(old, new).hex() == max(-1.0, min(1.0, expected)).hex()
    for values in ((0.0, 0.0), (2.0, 0.0), (math.inf, 0.0), (math.nan, 1.0)):
        with pytest.raises(ValueError):
            codec.decode_normalized(struct.pack("<2f", *values), dimensions=2)


@pytest.mark.parametrize("interrupted", [False, True])
def test_frozen_migration_keeps_original_ids_raw_values_and_rollback(tmp_path, interrupted):
    path = tmp_path / "original.sqlite3"

    def connect():
        connection = sqlite3.connect(path, isolation_level=None)
        connection.enable_load_extension(True)
        connection.load_extension(str(extension_path()))
        connection.enable_load_extension(False)
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    raw = connect()
    raw.executescript("""
        CREATE TABLE memory_facts(id INTEGER PRIMARY KEY);
        CREATE TABLE memory_embedding_profiles(id INTEGER PRIMARY KEY);
        CREATE TABLE memory_embeddings(
            id INTEGER PRIMARY KEY, fact_id INTEGER REFERENCES memory_facts(id),
            profile_id INTEGER REFERENCES memory_embedding_profiles(id),
            content_hash TEXT, vector_blob BLOB, created_at TEXT, updated_at TEXT);
        CREATE INDEX ix_memory_embeddings_profile_fact ON memory_embeddings(profile_id,fact_id);
        INSERT INTO memory_facts VALUES(11);
        INSERT INTO memory_embedding_profiles VALUES(7);
    """)
    original = struct.pack("<2f", -0.0, 1.0)
    raw.execute(
        "INSERT INTO memory_embeddings VALUES(31,11,7,?,?,?,?)",
        ("a" * 64, original, "original", "original"),
    )
    raw.close()
    source = Path(__file__).resolve().parents[2] / "migrations/versions/0153_memory_vec1.py"
    spec = importlib.util.spec_from_file_location("migration_vec1", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    engine = create_engine("sqlite://", creator=connect)

    def fail(_connection, _cursor, sql, *_args):
        if interrupted and sql == "DROP TABLE memory_embeddings_vec1_old":
            raise RuntimeError("migration interrupted")

    event.listen(engine, "before_cursor_execute", fail)
    try:
        with engine.connect() as connection:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            try:
                with patch.object(module.op, "get_bind", return_value=connection):
                    module.upgrade()
                connection.commit()
            except RuntimeError:
                connection.rollback()
                assert interrupted
    finally:
        engine.dispose()
    reopened = connect()
    columns = {row[1] for row in reopened.execute("PRAGMA table_info(memory_embeddings)")}
    if interrupted:
        assert "vector_blob" in columns
        assert (
            reopened.execute("SELECT vector_blob FROM memory_embeddings").fetchone()[0] == original
        )
        assert not reopened.execute(
            "SELECT 1 FROM sqlite_master WHERE name='memory_vectors_7'"
        ).fetchone()
    else:
        assert "vector_blob" not in columns
        assert reopened.execute("SELECT rowid,fact_id,vector FROM memory_vectors_7").fetchone() == (
            31,
            11,
            original,
        )
        assert reopened.execute("PRAGMA foreign_key_check").fetchall() == []
        reopened.execute("DELETE FROM memory_facts WHERE id=11")
        assert reopened.execute("SELECT count(*) FROM memory_vectors_7").fetchone()[0] == 0
    reopened.close()
