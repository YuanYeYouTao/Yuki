"""Original Social permission SQL, owned migration, and source-history boundaries."""

import asyncio
import hashlib
import importlib
import json
import sqlite3
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import event, insert, select, text
from tests.support.social_identity_cases import social_env

from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel
from qq_ai_bot.identity.canonical_repository import ensure_person
from qq_ai_bot.identity.db_models import CanonicalPersonModel
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.models import ChatEventModel
from qq_ai_bot.persistence.schema_guard import (
    CanonicalSchemaError,
    canonical_schema_revision,
    require_canonical_schema,
)
from qq_ai_bot.runtime.work_repository import WorkRepository
from qq_ai_bot.social.models import SocialError, SocialTarget

MIGRATION = "migrations.versions.0095_social_source_lookup_index"
INDEX = "ix_chat_events_social_source_author"


async def _add_sources(database, env, count):
    async with database.sessions() as session, session.begin():
        target_person = await ensure_person(session, "30001")
        original = dict((await session.execute(select(ChatEventModel.__table__))).mappings().one())
    original.pop("id")
    rows = [
        {
            **original,
            "canonical_event_id": str(uuid4()),
            "platform_message_id": f"fixture-history-{number}",
            "author_person_id": target_person if number == count - 1 else env.person,
        }
        for number in range(count)
    ]
    async with database.immediate_session() as session:
        await session.execute(insert(ChatEventModel), rows)
    return target_person, original


@pytest.mark.parametrize("history_count", [200, 2000, 20000])
async def test_actual_social_query_covering_plan_vm_and_pinned_source(
    database, tmp_path, history_count
):
    env = await social_env(database, tmp_path)
    person, _original = await _add_sources(database, env, history_count)
    target = SocialTarget(kind="person", id=person)
    statements = []

    def capture(_connection, _cursor, sql, parameters, *_args):
        statements.append((sql, parameters))

    event.listen(database.engine.sync_engine, "before_cursor_execute", capture)
    try:
        known = await env.service.check_target(target)
    finally:
        event.remove(database.engine.sync_engine, "before_cursor_execute", capture)
    queries = [(sql, args) for sql, args in statements if sql.startswith("SELECT chat_events.id ")]
    assert len(queries) == 1
    sql, parameters = queries[0]
    assert not any(
        statement.startswith(("INSERT", "UPDATE", "DELETE", "BEGIN IMMEDIATE"))
        for statement, _args in statements
    )
    assert await env.service.check_target(target, known_event_id=known) == known

    def measure():
        with sqlite3.connect(database.url.removeprefix("sqlite+aiosqlite:///")) as db:
            before = db.execute("SELECT * FROM chat_events ORDER BY id").fetchall()
            db.execute(f'DROP INDEX "{INDEX}"')
            results = []
            for indexed in (False, True):
                if indexed:
                    db.execute(importlib.import_module(MIGRATION).INDEX_SQL)
                plan = [row[3] for row in db.execute("EXPLAIN QUERY PLAN " + sql, parameters)]
                steps = 0

                def progress():
                    nonlocal steps
                    steps += 1
                    return 0

                db.set_progress_handler(progress, 1)
                try:
                    actual = db.execute(sql, parameters).fetchall()
                finally:
                    db.set_progress_handler(None, 0)
                assert actual == [(known,)]
                results.append((plan, steps))
            assert db.execute("SELECT * FROM chat_events ORDER BY id").fetchall() == before
            db.commit()
            return results

    original, indexed = await asyncio.to_thread(measure)
    assert "SCAN chat_events" in original[0]
    assert any(f"COVERING INDEX {INDEX}" in plan for plan in indexed[0])
    assert not any("TEMP B-TREE" in plan for plan in indexed[0])
    assert original[1] > history_count * 2
    assert indexed[1] < 40


@pytest.mark.parametrize(
    "case",
    [
        "present",
        "suppressed",
        "old_generation",
        "other_conversation",
        "outbound",
        "external_bot",
        "null_author",
        "disabled",
        "missing_person",
        "no_source",
        "wrong_pin",
    ],
)
async def test_existing_person_source_permission_is_identical_without_index(
    database, tmp_path, case
):
    env = await social_env(database, tmp_path)
    person, _original = await _add_sources(database, env, 1)
    async with database.sessions() as session, session.begin():
        row = await session.scalar(
            select(ChatEventModel).where(ChatEventModel.author_person_id == person)
        )
        source_id = row.id
        if case == "suppressed":
            row.suppression_status = "duplicate"
            row.utterance_fingerprint = "synthetic-duplicate"
        elif case == "old_generation":
            conversation = await session.get(
                CanonicalConversationModel, row.canonical_conversation_id
            )
            conversation.generation += 1
            conversation.starts_after_event_id = source_id
            conversation.covered_through_event_id = source_id
            conversation.last_event_id = source_id
            conversation.last_generation_change_event_id = source_id
        elif case == "other_conversation":
            # A group source can establish this contact independently of private generation.
            assert row.canonical_conversation_id == env.context.conversation_id
        elif case == "outbound":
            row.direction = "outbound"
        elif case in {"external_bot", "null_author"}:
            row.author_kind, row.author_person_id = "external_bot", None
        elif case == "disabled":
            found = await session.get(CanonicalPersonModel, person)
            found.enabled = False
    if case in {"missing_person", "no_source"}:
        if case == "missing_person":
            person = str(uuid4())
        else:
            async with database.sessions() as session, session.begin():
                person = await ensure_person(session, "40001")
    target = SocialTarget(kind="person", id=person)

    async def outcome():
        try:
            return await env.service.check_target(
                target, known_event_id=source_id + 1 if case == "wrong_pin" else None
            )
        except SocialError as exc:
            return str(exc)

    before = await outcome()
    async with database.engine.begin() as connection:
        await connection.execute(text(f'DROP INDEX "{INDEX}"'))
    assert await outcome() == before
    assert (isinstance(before, int)) == (
        case in {"present", "suppressed", "old_generation", "other_conversation"}
    )


def _replace_index(connection, shape):
    connection.exec_driver_sql(f'DROP INDEX "{INDEX}"')
    if shape == "missing":
        return
    if shape in {"table", "view"}:
        definition = "(unrelated INT)" if shape == "table" else "AS SELECT 1 AS unrelated"
        connection.exec_driver_sql(f'CREATE {shape.upper()} "{INDEX}" {definition}')
        return
    table = "chat_events"
    if shape == "wrong_table":
        table = "unrelated_events"
        connection.exec_driver_sql(
            f"CREATE TABLE {table}(id INT,author_person_id TEXT,direction TEXT,author_kind TEXT)"
        )
    key = {
        "column": "canonical_conversation_id,id",
        "expression": "author_person_id||'',id",
        "descending": "author_person_id DESC,id",
        "collation": "author_person_id COLLATE NOCASE,id",
        "order": "id,author_person_id",
    }.get(shape, "author_person_id,id")
    predicate = {
        "predicate": "direction='outbound' AND author_kind='person'",
        "case": "direction='INBOUND' AND author_kind='person'",
        "literal_space": "direction='in bound' AND author_kind='person'",
        "literal_double_quote": "direction='in\"bound' AND author_kind='person'",
        "literal_escape": "direction='in''bound' AND author_kind='person'",
        "author": "direction='inbound' AND author_kind='yuki'",
    }.get(shape, "direction='inbound' AND author_kind='person'")
    suffix = "" if shape == "nonpartial" else f" WHERE {predicate}"
    unique = "UNIQUE " if shape == "unique" else ""
    connection.exec_driver_sql(f'CREATE {unique}INDEX "{INDEX}" ON {table}({key}){suffix}')


SHAPES = (
    "table",
    "view",
    "wrong_table",
    "column",
    "expression",
    "descending",
    "collation",
    "order",
    "unique",
    "nonpartial",
    "predicate",
    "case",
    "literal_space",
    "literal_double_quote",
    "literal_escape",
    "author",
)


@pytest.mark.parametrize("action", ["upgrade", "downgrade"])
@pytest.mark.parametrize("shape", SHAPES)
async def test_owned_drift_fails_before_ddl(database, monkeypatch, action, shape):
    migration = importlib.import_module(MIGRATION)

    def exercise(connection):
        monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
        _replace_index(connection, shape)
        before = connection.exec_driver_sql(
            "SELECT type,name,sql FROM sqlite_master ORDER BY type,name"
        ).all()
        statements = []

        def capture(_connection, _cursor, sql, *_args):
            statements.append(sql.lstrip().upper())

        event.listen(connection, "before_cursor_execute", capture)
        try:
            with pytest.raises(RuntimeError, match="index shape mismatch"):
                getattr(migration, action)()
        finally:
            event.remove(connection, "before_cursor_execute", capture)
        assert not any(sql.startswith(("CREATE", "DROP", "ALTER")) for sql in statements)
        assert (
            connection.exec_driver_sql(
                "SELECT type,name,sql FROM sqlite_master ORDER BY type,name"
            ).all()
            == before
        )

    async with database.engine.begin() as connection:
        await connection.run_sync(exercise)


@pytest.mark.parametrize("shape", ("missing", *SHAPES))
async def test_startup_rejects_social_source_index_drift(database, shape):
    async with database.engine.begin() as connection:
        await connection.execute(text("CREATE TABLE alembic_version(version_num VARCHAR(32))"))
        await connection.execute(
            text("INSERT INTO alembic_version VALUES (:revision)"),
            {"revision": canonical_schema_revision()},
        )
        await connection.run_sync(lambda sync: _replace_index(sync, shape))
    with pytest.raises(CanonicalSchemaError, match="social source index"):
        await require_canonical_schema(database.url)


async def test_repeat_upgrade_and_downgrade_owns_only_its_index(database, monkeypatch):
    migration = importlib.import_module(MIGRATION)

    def exercise(connection):
        monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
        before = connection.exec_driver_sql('PRAGMA index_list("chat_events")').all()
        migration.upgrade()
        migration.upgrade()
        assert connection.exec_driver_sql('PRAGMA index_list("chat_events")').all() == before
        migration.downgrade()
        remaining = connection.exec_driver_sql('PRAGMA index_list("chat_events")').all()
        assert {row[1] for row in remaining} == {row[1] for row in before} - {INDEX}
        with pytest.raises(RuntimeError, match="index missing"):
            migration.downgrade()
        assert connection.exec_driver_sql('PRAGMA index_list("chat_events")').all() == remaining
        migration.upgrade()

    async with database.engine.begin() as connection:
        await connection.run_sync(exercise)


@pytest.mark.parametrize("shape", ["missing", "view", "columns"])
async def test_invalid_target_table_fails_before_ddl(tmp_path, monkeypatch, shape):
    migration = importlib.import_module(MIGRATION)
    from sqlalchemy import create_engine

    engine = create_engine(f"sqlite:///{tmp_path / 'shape.db'}")
    try:
        with engine.begin() as connection:
            monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
            if shape == "view":
                connection.exec_driver_sql(
                    "CREATE VIEW chat_events AS SELECT 1 AS id,'person' AS author_kind,"
                    "'inbound' AS direction,'id' AS author_person_id"
                )
            elif shape == "columns":
                connection.exec_driver_sql("CREATE TABLE chat_events(id INT,author_person_id TEXT)")
            before = connection.exec_driver_sql(
                "SELECT type,name,sql FROM sqlite_master ORDER BY type,name"
            ).all()
            with pytest.raises(RuntimeError, match="table shape mismatch"):
                migration.upgrade()
            assert (
                connection.exec_driver_sql(
                    "SELECT type,name,sql FROM sqlite_master ORDER BY type,name"
                ).all()
                == before
            )
    finally:
        engine.dispose()


def _database_facts(path):
    with sqlite3.connect(path) as db:
        tables = [
            row[0]
            for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT IN ('alembic_version','sqlite_sequence') ORDER BY name"
            )
        ]
        facts = {}
        for table in tables:
            rows = db.execute(f'SELECT * FROM "{table}"').fetchall()
            serialized = sorted(json.dumps(row, default=str, ensure_ascii=False) for row in rows)
            facts[table] = (len(rows), hashlib.sha256(json.dumps(serialized).encode()).hexdigest())
        schema = db.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name"
        ).fetchall()
        return facts, schema, db.execute("SELECT version_num FROM alembic_version").fetchone()[0]


async def test_actual_0094_upgrade_downgrade_preserves_all_business_facts(tmp_path, monkeypatch):
    path = tmp_path / "migration.sqlite3"
    url = f"sqlite+aiosqlite:///{path.as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    config = Config("alembic.ini")
    await asyncio.to_thread(command.upgrade, config, "0094")
    # Historical bootstrap may declare current metadata; emulate a deployed old head.
    with sqlite3.connect(path) as db:
        db.execute(f'DROP INDEX IF EXISTS "{INDEX}"')
    original = Database(url)
    try:
        env = await social_env(original, tmp_path)
        repository = WorkRepository(original)
        lease = await repository.acquire(env.context.conversation_id, 1)
        assert lease
        accepted = await repository.accept(
            lease, source_key="original-work", source={}, goal="preserved"
        )
        await repository.checkpoint(lease, accepted["id"], None, models=3, tools=2)
        await repository.enqueue(
            env.context.conversation_id,
            1,
            "original-input",
            kind="message",
            work_id=accepted["id"],
            ready=False,
        )
    finally:
        await original.close()
    before_facts, before_schema, version = _database_facts(path)
    assert version == "0094"
    # The merged head is now 0096. Keep this owned 0095 DDL/downgrade check
    # pinned to 0095; verify the full current chain separately below.
    await asyncio.to_thread(command.upgrade, config, "0095")
    with pytest.raises(CanonicalSchemaError, match="migration head"):
        await require_canonical_schema(url)
    after_facts, after_schema, version = _database_facts(path)
    assert version == "0095"
    assert after_facts == before_facts
    assert [row for row in after_schema if row[1] != INDEX] == before_schema
    await asyncio.to_thread(command.downgrade, config, "0094")
    downgraded_facts, downgraded_schema, version = _database_facts(path)
    assert version == "0094"
    assert downgraded_facts == before_facts and downgraded_schema == before_schema
    with pytest.raises(CanonicalSchemaError, match="migration head"):
        await require_canonical_schema(url)
    await asyncio.to_thread(command.upgrade, config, "head")
    await require_canonical_schema(url)
    final_facts, _final_schema, version = _database_facts(path)
    assert version == canonical_schema_revision()
    assert final_facts == before_facts
