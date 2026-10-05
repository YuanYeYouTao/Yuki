"""Actual recovery SQL, SQLite work, writer races, and owned index migration."""

import asyncio
import importlib
import json
import sqlite3
import time
from contextlib import asynccontextmanager

import pytest
from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import event, insert, select, text, update
from tests.support.social_identity_cases import social_env

from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.models import ChatEventModel
from qq_ai_bot.persistence.schema_guard import (
    CanonicalSchemaError,
    canonical_schema_revision,
    require_canonical_schema,
)
from qq_ai_bot.runtime.work_repository import WorkRepository
from qq_ai_bot.runtime.work_schema_v1 import inputs, work

MIGRATION = "migrations.versions.0094_work_abandoned_input_index"
INDEX = "ix_runtime_work_inputs_abandoned"


async def _capture_discovery(database, repository):
    statements = []

    def capture(_connection, _cursor, statement, parameters, *_args):
        statements.append((statement, parameters))

    event.listen(database.engine.sync_engine, "before_cursor_execute", capture)
    try:
        await repository.repair_abandoned_inputs("current-process")
    finally:
        event.remove(database.engine.sync_engine, "before_cursor_execute", capture)
    discovery = [
        (sql, parameters)
        for sql, parameters in statements
        if sql.startswith("SELECT runtime_work_inputs.id ") and "ready IS 0" in sql
    ]
    assert len(discovery) == 1
    return discovery[0], statements


@pytest.mark.parametrize("history_count", [200, 2000, 20000])
async def test_actual_idle_recovery_query_plan_and_vm_work(database, tmp_path, history_count):
    env = await social_env(database, tmp_path)
    repository = WorkRepository(database)
    now = time.time()
    rows = [
        dict(
            conversation_id=env.context.conversation_id,
            generation=1,
            source_key=f"history-{number}",
            kind="message",
            state="consumed" if number % 2 else "cancelled",
            ready=True,
            prepare_owner=None,
            payload_json='{"text":"retained original input"}',
            created=now - 86400,
        )
        for number in range(history_count)
    ]
    # Current-process preparation is still live; unknown NULL ownership is not
    # automatically foreign ownership. Neither is eligible while younger than 120s.
    rows.extend(
        {
            **rows[0],
            "source_key": f"live-{number}",
            "state": "pending",
            "ready": False,
            "prepare_owner": "current-process" if number % 2 else None,
            "created": now,
        }
        for number in range(8)
    )
    async with database.immediate_session() as writer:
        await writer.execute(insert(inputs), rows)
    (statement, parameters), statements = await _capture_discovery(database, repository)
    assert not any(
        sql.lstrip().upper().startswith(("BEGIN IMMEDIATE", "INSERT", "UPDATE", "DELETE"))
        for sql, _parameters in statements
    )
    path = database.url.removeprefix("sqlite+aiosqlite:///")

    def measure():
        with sqlite3.connect(path) as db:
            before = db.execute("SELECT * FROM runtime_work_inputs ORDER BY id").fetchall()
            db.execute(f'DROP INDEX "{INDEX}"')
            measured = []
            for indexed in (False, True):
                if indexed:
                    db.execute(importlib.import_module(MIGRATION).INDEX_SQL)
                plan = [row[3] for row in db.execute("EXPLAIN QUERY PLAN " + statement, parameters)]
                steps = 0

                def progress():
                    nonlocal steps
                    steps += 1
                    return 0

                db.set_progress_handler(progress, 1)
                try:
                    actual = db.execute(statement, parameters).fetchall()
                finally:
                    db.set_progress_handler(None, 0)
                assert actual == []
                measured.append((indexed, plan, steps))
            assert db.execute("SELECT * FROM runtime_work_inputs ORDER BY id").fetchall() == before
            db.commit()
            return measured

    measured = await asyncio.to_thread(measure)
    assert "SCAN runtime_work_inputs" in measured[0][1]
    assert any(INDEX in line for line in measured[1][1])
    assert measured[1][2] < 150
    assert measured[0][2] > history_count * 3
    assert measured[0][2] > measured[1][2] * 5
    print(
        f"abandoned_inputs history={history_count} unindexed_vm={measured[0][2]} "
        f"indexed_vm={measured[1][2]}"
    )


@pytest.mark.parametrize("indexed", [False, True])
async def test_repair_keeps_original_owner_age_state_ids_and_budgets(database, tmp_path, indexed):
    env = await social_env(database, tmp_path)
    repository = WorkRepository(database)
    lease = await repository.acquire(env.context.conversation_id, 1)
    assert lease
    accepted = await repository.accept(
        lease, source_key="original-work", source={}, goal="original"
    )
    await repository.checkpoint(lease, accepted["id"], None, models=3, tools=2)
    now = time.time()
    specifications = [
        ("foreign", "pending", False, "other-process", now, True, "pending", True),
        ("current-live", "pending", False, "current-process", now, True, "pending", False),
        ("current-old", "pending", False, "current-process", now - 121, True, "pending", True),
        ("unknown-live", "pending", False, None, now, True, "pending", False),
        ("unknown-old", "pending", False, None, now - 121, True, "pending", True),
        ("ready", "pending", True, "other-process", now, True, "pending", True),
        ("staged", "staged", False, "other-process", now, True, "staged", False),
        ("consumed", "consumed", False, "other-process", now, True, "consumed", False),
        ("cancelled", "cancelled", False, "other-process", now, True, "cancelled", False),
        ("missing-event", "pending", False, "other-process", now, False, "cancelled", False),
    ]
    async with database.immediate_session() as writer:
        if not indexed:
            await writer.execute(text(f'DROP INDEX "{INDEX}"'))
        event_id = await writer.scalar(
            select(ChatEventModel.id).where(
                ChatEventModel.canonical_conversation_id == env.context.conversation_id
            )
        )
        await writer.execute(
            update(work).where(work.c.id == accepted["id"]).values(state="waiting_user")
        )
        for (
            key,
            state,
            ready,
            owner,
            created,
            has_event,
            _final_state,
            _final_ready,
        ) in specifications:
            await writer.execute(
                insert(inputs).values(
                    conversation_id=env.context.conversation_id,
                    generation=1,
                    source_key=key,
                    work_id=accepted["id"],
                    kind="message",
                    state=state,
                    ready=ready,
                    prepare_owner=owner,
                    event_id=event_id if has_event else None,
                    payload_json='{"text":"original preparation"}',
                    created=created,
                )
            )
    async with database.sessions() as reader:
        before = (await reader.execute(select(inputs).order_by(inputs.c.id))).mappings().all()
    await repository.repair_abandoned_inputs("current-process")
    async with database.sessions() as reader:
        after = (await reader.execute(select(inputs).order_by(inputs.c.id))).mappings().all()
    assert len(before) == len(after) == len(specifications)
    for previous, saved, specification in zip(before, after, specifications, strict=True):
        assert saved["state"] == specification[-2] and saved["ready"] == specification[-1]
        for field in (
            "id",
            "source_key",
            "work_id",
            "event_id",
            "prepare_owner",
            "created",
            "generation",
        ):
            assert saved[field] == previous[field]
        if saved["source_key"] in {"foreign", "current-old", "unknown-old"}:
            assert "hello" in json.loads(saved["payload_json"])["text"]
        else:
            assert saved["payload_json"] == previous["payload_json"]
    restored = await repository.get(accepted["id"])
    assert restored["state"] == "queued"
    assert (restored["model_requests"], restored["tool_calls"], restored["sent_messages"]) == (
        3,
        2,
        0,
    )
    (statement, _parameters), statements = await _capture_discovery(database, repository)
    assert "ready IS 0" in statement
    assert not any(sql.upper().startswith("UPDATE") for sql, _ in statements)


@pytest.mark.parametrize("change", ["ready", "owner", "state"])
async def test_writer_rechecks_input_prepared_after_discovery(
    database, tmp_path, monkeypatch, change
):
    env = await social_env(database, tmp_path)
    repository = WorkRepository(database)
    identity = await repository.enqueue(
        env.context.conversation_id, 1, "race", kind="message", ready=False
    )
    async with database.immediate_session() as writer:
        await writer.execute(
            update(inputs).where(inputs.c.id == identity).values(prepare_owner="other-process")
        )
    original = database.immediate_session
    injected = False

    @asynccontextmanager
    async def raced():
        nonlocal injected
        if not injected:
            injected = True
            async with original() as writer:
                values = {
                    "ready": {"ready": True},
                    "owner": {"prepare_owner": "current-process"},
                    "state": {"state": "cancelled"},
                }[change]
                await writer.execute(
                    update(inputs)
                    .where(inputs.c.id == identity)
                    .values(**values, payload_json='{"text":"real completed preparation"}')
                )
        async with original() as writer:
            yield writer

    monkeypatch.setattr(database, "immediate_session", raced)
    await repository.repair_abandoned_inputs("current-process")
    assert injected
    async with database.sessions() as reader:
        saved = (
            (await reader.execute(select(inputs).where(inputs.c.id == identity))).mappings().one()
        )
    assert saved["state"] == ("cancelled" if change == "state" else "pending")
    assert saved["ready"] == (change == "ready")
    assert saved["prepare_owner"] == ("current-process" if change == "owner" else "other-process")
    assert json.loads(saved["payload_json"])["text"] == "real completed preparation"


async def test_idle_reader_completes_with_unrelated_wal_writer(database, tmp_path):
    env = await social_env(database, tmp_path)
    repository = WorkRepository(database)
    async with database.immediate_session() as writer:
        await writer.execute(
            insert(inputs).values(
                conversation_id=env.context.conversation_id,
                generation=1,
                source_key="uncommitted-history",
                kind="message",
                state="consumed",
                ready=True,
                created=time.time(),
            )
        )
        _query, statements = await asyncio.wait_for(
            _capture_discovery(database, repository), timeout=1
        )
    assert not any(
        sql.lstrip().upper().startswith(("BEGIN IMMEDIATE", "INSERT", "UPDATE", "DELETE"))
        for sql, _ in statements
    )


async def test_recovery_retains_128_page_and_continues_remaining_input(database, tmp_path):
    env = await social_env(database, tmp_path)
    repository = WorkRepository(database)
    async with database.immediate_session() as writer:
        await writer.execute(
            insert(inputs),
            [
                dict(
                    conversation_id=env.context.conversation_id,
                    generation=1,
                    source_key=f"foreign-{number}",
                    kind="message",
                    state="pending",
                    ready=False,
                    prepare_owner="other-process",
                    created=time.time(),
                )
                for number in range(129)
            ],
        )
    async with database.sessions() as reader:
        original_ids = tuple(await reader.scalars(select(inputs.c.id).order_by(inputs.c.id)))
    await repository.repair_abandoned_inputs("current-process")
    async with database.sessions() as reader:
        states = (await reader.execute(select(inputs.c.id, inputs.c.state))).all()
    assert sum(row.state == "cancelled" for row in states) == 128
    assert sum(row.state == "pending" for row in states) == 1
    await repository.repair_abandoned_inputs("current-process")
    async with database.sessions() as reader:
        states = (
            await reader.execute(select(inputs.c.id, inputs.c.state).order_by(inputs.c.id))
        ).all()
    assert tuple(row.id for row in states) == original_ids
    assert all(row.state == "cancelled" for row in states)


async def test_committed_recovery_with_lost_ack_is_not_replayed(database, tmp_path, monkeypatch):
    env = await social_env(database, tmp_path)
    repository = WorkRepository(database)
    async with database.sessions() as reader:
        event_id = await reader.scalar(
            select(ChatEventModel.id).where(
                ChatEventModel.canonical_conversation_id == env.context.conversation_id
            )
        )
    identity = await repository.enqueue(
        env.context.conversation_id,
        1,
        "original-input",
        kind="message",
        event_id=event_id,
        ready=False,
    )
    async with database.immediate_session() as writer:
        await writer.execute(
            update(inputs).where(inputs.c.id == identity).values(prepare_owner="other-process")
        )
    original = database.immediate_session

    @asynccontextmanager
    async def lost_confirmation():
        async with original() as writer:
            yield writer
        raise OSError("synthetic commit acknowledgement lost")

    monkeypatch.setattr(database, "immediate_session", lost_confirmation)
    with pytest.raises(OSError, match="acknowledgement lost"):
        await repository.repair_abandoned_inputs("current-process")
    monkeypatch.setattr(database, "immediate_session", original)
    async with database.sessions() as reader:
        saved = (
            (await reader.execute(select(inputs).where(inputs.c.id == identity))).mappings().one()
        )
    assert saved["id"] == identity and saved["ready"] and saved["state"] == "pending"
    assert "hello" in json.loads(saved["payload_json"])["text"]
    _query, statements = await _capture_discovery(database, repository)
    assert not any(sql.lstrip().upper().startswith("UPDATE") for sql, _ in statements)
    async with database.sessions() as reader:
        repeated = (
            (await reader.execute(select(inputs).where(inputs.c.id == identity))).mappings().one()
        )
    assert dict(repeated) == dict(saved)


def _replace_index(connection, shape):
    connection.exec_driver_sql(f'DROP INDEX "{INDEX}"')
    if shape == "missing":
        return
    if shape in {"table", "view"}:
        definition = "(unrelated INT)" if shape == "table" else "AS SELECT 1 AS unrelated"
        connection.exec_driver_sql(f'CREATE {shape.upper()} "{INDEX}" {definition}')
        return
    table = "runtime_work_inputs"
    if shape == "wrong_table":
        table = "unrelated_inputs"
        connection.exec_driver_sql(f"CREATE TABLE {table}(id INT,state TEXT,ready INT)")
    key = {
        "column": "created",
        "expression": "id+0",
        "descending": "id DESC",
        "collation": "id COLLATE NOCASE",
    }.get(shape, "id")
    predicate = {
        "predicate": "state = 'consumed' AND ready IS 0",
        "case": "state = 'PENDING' AND ready IS 0",
        "literal_space": "state = 'pen ding' AND ready IS 0",
        "literal_double_quote": "state = 'p\"ending' AND ready IS 0",
        "literal_escape": "state = 'pend''ing' AND ready IS 0",
        "ready": "state = 'pending' AND ready IS 1",
    }.get(shape, "state = 'pending' AND ready IS 0")
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
    "unique",
    "nonpartial",
    "predicate",
    "case",
    "literal_space",
    "literal_double_quote",
    "literal_escape",
    "ready",
)


@pytest.mark.parametrize("action", ["upgrade", "downgrade"])
@pytest.mark.parametrize("shape", SHAPES)
async def test_bad_owned_shape_rejected_before_first_ddl(database, monkeypatch, action, shape):
    migration = importlib.import_module(MIGRATION)

    def exercise(connection):
        monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
        _replace_index(connection, shape)
        before = connection.exec_driver_sql(
            "SELECT type,name,sql FROM sqlite_master ORDER BY type,name"
        ).all()
        statements = []

        def capture(_connection, _cursor, statement, *_args):
            statements.append(statement.lstrip().upper())

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
async def test_startup_rejects_repair_index_drift(database, shape):
    async with database.engine.begin() as connection:
        await connection.execute(text("CREATE TABLE alembic_version(version_num VARCHAR(32))"))
        await connection.execute(
            text("INSERT INTO alembic_version VALUES (:revision)"),
            {"revision": canonical_schema_revision()},
        )
        await connection.run_sync(lambda sync: _replace_index(sync, shape))
    with pytest.raises(CanonicalSchemaError, match="abandoned input index"):
        await require_canonical_schema(database.url)


async def test_repeated_upgrade_and_missing_downgrade_preserve_other_indexes(database, monkeypatch):
    migration = importlib.import_module(MIGRATION)

    def exercise(connection):
        monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
        before = connection.exec_driver_sql('PRAGMA index_list("runtime_work_inputs")').all()
        migration.upgrade()
        migration.upgrade()
        assert (
            connection.exec_driver_sql('PRAGMA index_list("runtime_work_inputs")').all() == before
        )
        migration.downgrade()
        remaining = connection.exec_driver_sql('PRAGMA index_list("runtime_work_inputs")').all()
        assert {row[1] for row in remaining} == {row[1] for row in before} - {INDEX}
        with pytest.raises(RuntimeError, match="index missing"):
            migration.downgrade()
        assert (
            connection.exec_driver_sql('PRAGMA index_list("runtime_work_inputs")').all()
            == remaining
        )
        migration.upgrade()

    async with database.engine.begin() as connection:
        await connection.run_sync(exercise)


@pytest.mark.parametrize("shape", ["missing", "view", "columns"])
async def test_target_table_shape_is_rejected_before_ddl(database, monkeypatch, shape):
    migration = importlib.import_module(MIGRATION)

    def exercise(connection):
        monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
        connection.exec_driver_sql('DROP TABLE "runtime_work_inputs"')
        if shape == "view":
            connection.exec_driver_sql(
                "CREATE VIEW runtime_work_inputs AS SELECT 1 AS id,'pending' AS state,0 AS ready"
            )
        elif shape == "columns":
            connection.exec_driver_sql("CREATE TABLE runtime_work_inputs(id INT,state TEXT)")
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

    async with database.engine.begin() as connection:
        await connection.run_sync(exercise)


async def test_real_upgrade_downgrade_preserves_original_work_and_inputs(tmp_path, monkeypatch):
    path = tmp_path / "migration.sqlite3"
    url = f"sqlite+aiosqlite:///{path.as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    config = Config("alembic.ini")
    await asyncio.to_thread(command.upgrade, config, "0093")
    # Historical fresh-install fixtures can declare current metadata indexes.
    # A real deployed 0093 database predates this index; exercise its CREATE path.
    with sqlite3.connect(path) as db:
        db.execute(f'DROP INDEX IF EXISTS "{INDEX}"')
    original_database = Database(url)
    try:
        env = await social_env(original_database, tmp_path)
        repository = WorkRepository(original_database)
        lease = await repository.acquire(env.context.conversation_id, 1)
        assert lease
        original_work = await repository.accept(
            lease, source_key="original-work", source={}, goal="keep original"
        )
        await repository.checkpoint(lease, original_work["id"], None, models=3, tools=2)
        await repository.enqueue(
            env.context.conversation_id,
            1,
            "original-input",
            kind="message",
            work_id=original_work["id"],
            ready=False,
        )
    finally:
        await original_database.close()
    with sqlite3.connect(path) as db:
        before = db.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name"
        ).fetchall()
        retained = {
            table: db.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()
            for table in ("runtime_work", "runtime_work_inputs")
        }
    # This regression owns revision 0094; later indexes have separate migration tests.
    await asyncio.to_thread(command.upgrade, config, "0094")
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT version_num FROM alembic_version").fetchone()[0] == "0094"
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        after = db.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name"
        ).fetchall()
        assert [row for row in after if row[1] != INDEX] == before
        assert {
            table: db.execute(f"SELECT * FROM {table} ORDER BY id").fetchall() for table in retained
        } == retained
    await asyncio.to_thread(command.downgrade, config, "0093")
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT version_num FROM alembic_version").fetchone()[0] == "0093"
        assert (
            db.execute(
                "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name"
            ).fetchall()
            == before
        )
        assert {
            table: db.execute(f"SELECT * FROM {table} ORDER BY id").fetchall() for table in retained
        } == retained
    await asyncio.to_thread(command.upgrade, config, "head")
    await require_canonical_schema(url)
