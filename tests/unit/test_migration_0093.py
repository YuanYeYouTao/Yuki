"""Compaction index migration owns one exact shape and preserves domain storage."""

import asyncio
import importlib
import sqlite3

import pytest
from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import event, text

from qq_ai_bot.persistence.schema_guard import (
    CanonicalSchemaError,
    canonical_schema_revision,
    require_canonical_schema,
)

MIGRATION = "migrations.versions.0093_evidence_compaction_candidate_index"


async def test_compaction_fresh_install_upgrade_downgrade_preserves_schema(
    database, tmp_path, monkeypatch
):
    migration = importlib.import_module(MIGRATION)
    assert migration.down_revision == "0092" and migration.revision == "0093"
    path = tmp_path / "compaction-migration.sqlite3"
    url = f"sqlite+aiosqlite:///{path.as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    config = Config("alembic.ini")
    await asyncio.to_thread(command.upgrade, config, "0092")
    with sqlite3.connect(path) as db:
        db.execute(
            "INSERT INTO memory_evidence_compaction_runs "
            "(public_id,status,scan_after_fact_id,scanned_facts,completed_items,skipped_items,"
            "failed_items,evidence_before,evidence_after,created_at,updated_at) "
            "VALUES ('original-run','running',9,3,1,1,1,13,8,'2026-10-04','2026-10-04')"
        )
        db.commit()
        before = db.execute("SELECT * FROM memory_evidence_compaction_runs").fetchall()
        schema = db.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master "
            "WHERE tbl_name LIKE 'memory_%' ORDER BY type,name"
        ).fetchall()
    await asyncio.to_thread(command.upgrade, config, "head")
    await require_canonical_schema(url)
    name, table, _columns = migration.INDEXES[0]
    async with database.engine.connect() as connection:
        metadata_sql = await connection.scalar(
            text("SELECT sql FROM sqlite_master WHERE name=:name"), {"name": name}
        )
    with sqlite3.connect(path) as db:
        assert (
            db.execute("SELECT sql FROM sqlite_master WHERE name=?", (name,)).fetchone()[0]
            == metadata_sql
        )
        assert db.execute("SELECT * FROM memory_evidence_compaction_runs").fetchall() == before
        keys = [row[2:5] for row in db.execute(f'PRAGMA index_xinfo("{name}")') if row[5]]
        assert keys == [
            ("fact_id", 0, "BINARY"),
            ("evidence_before", 0, "BINARY"),
            ("status", 0, "BINARY"),
        ]
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    await asyncio.to_thread(command.downgrade, config, "0092")
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT * FROM memory_evidence_compaction_runs").fetchall() == before
        assert (
            db.execute(
                "SELECT type,name,tbl_name,sql FROM sqlite_master "
                "WHERE tbl_name LIKE 'memory_%' ORDER BY type,name"
            ).fetchall()
            == schema
        )
        assert db.execute(f'PRAGMA foreign_key_list("{table}")').fetchall()
    await asyncio.to_thread(command.upgrade, config, "head")
    await require_canonical_schema(url)


def _replace_index(connection, migration, shape):
    name, table, columns = migration.INDEXES[0]
    connection.exec_driver_sql(f'DROP INDEX "{name}"')
    if shape == "missing":
        return
    if shape in {"table", "view"}:
        definition = "(unrelated TEXT)" if shape == "table" else "AS SELECT 1 AS unrelated"
        connection.exec_driver_sql(f'CREATE {shape.upper()} "{name}" {definition}')
        return
    if shape == "wrong_table":
        table = "unrelated_compaction_items"
        connection.exec_driver_sql(
            f"CREATE TABLE {table}(fact_id INT,evidence_before INT,status TEXT)"
        )
    keys = {
        "columns": "fact_id,status",
        "order": "evidence_before,fact_id,status",
        "descending": "fact_id,evidence_before,status DESC",
        "collation": "fact_id,evidence_before,status COLLATE NOCASE",
        "expression": "fact_id+0,evidence_before,status",
    }.get(shape, ",".join(columns))
    unique = "UNIQUE " if shape == "unique" else ""
    predicate = " WHERE status='completed'" if shape == "partial" else ""
    connection.exec_driver_sql(f'CREATE {unique}INDEX "{name}" ON "{table}"({keys}){predicate}')


@pytest.mark.parametrize("action", ["upgrade", "downgrade"])
@pytest.mark.parametrize(
    "shape",
    [
        "columns",
        "order",
        "unique",
        "partial",
        "descending",
        "collation",
        "expression",
        "wrong_table",
        "table",
        "view",
    ],
)
async def test_compaction_bad_owned_shapes_rejected_before_ddl(
    database, monkeypatch, action, shape
):
    migration = importlib.import_module(MIGRATION)
    statements = []

    def observe(_connection, _cursor, statement, *_args):
        statements.append(statement.lstrip().upper())

    def exercise(connection):
        monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
        _replace_index(connection, migration, shape)
        before = connection.exec_driver_sql(
            "SELECT type,name,sql FROM sqlite_master ORDER BY type,name"
        ).all()
        event.listen(connection, "before_cursor_execute", observe)
        try:
            with pytest.raises(RuntimeError, match="index shape mismatch"):
                getattr(migration, action)()
        finally:
            event.remove(connection, "before_cursor_execute", observe)
        assert not any(
            statement.startswith(("CREATE", "DROP", "ALTER")) for statement in statements
        )
        assert (
            connection.exec_driver_sql(
                "SELECT type,name,sql FROM sqlite_master ORDER BY type,name"
            ).all()
            == before
        )

    async with database.engine.begin() as connection:
        await connection.run_sync(exercise)


@pytest.mark.parametrize(
    "shape",
    [
        "missing",
        "columns",
        "order",
        "unique",
        "partial",
        "descending",
        "collation",
        "expression",
        "wrong_table",
        "table",
        "view",
    ],
)
async def test_startup_rejects_compaction_index_drift(database, monkeypatch, shape):
    migration = importlib.import_module(MIGRATION)
    async with database.engine.begin() as connection:
        await connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32))"))
        await connection.execute(
            text("INSERT INTO alembic_version VALUES (:revision)"),
            {"revision": canonical_schema_revision()},
        )
        await connection.run_sync(lambda connection: _replace_index(connection, migration, shape))
    with pytest.raises(CanonicalSchemaError, match="evidence compaction index"):
        await require_canonical_schema(database.url)


@pytest.mark.parametrize("shape", ["missing", "view", "columns"])
async def test_compaction_target_table_prevalidated(database, monkeypatch, shape):
    migration = importlib.import_module(MIGRATION)

    def exercise(connection):
        monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
        _name, table, _columns = migration.INDEXES[0]
        connection.exec_driver_sql(f'DROP TABLE "{table}"')
        if shape == "view":
            connection.exec_driver_sql(
                f'CREATE VIEW "{table}" AS SELECT 1 AS fact_id,2 AS evidence_before,3 AS status'
            )
        elif shape == "columns":
            connection.exec_driver_sql(f'CREATE TABLE "{table}" (fact_id INT,status TEXT)')
        with pytest.raises(RuntimeError, match="table shape mismatch"):
            migration.upgrade()

    async with database.engine.begin() as connection:
        await connection.run_sync(exercise)


async def test_compaction_repeated_upgrade_and_missing_downgrade_preserve_other_indexes(
    database, monkeypatch
):
    migration = importlib.import_module(MIGRATION)

    def exercise(connection):
        monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
        name, table, _columns = migration.INDEXES[0]
        before = connection.exec_driver_sql(f'PRAGMA index_list("{table}")').all()
        migration.upgrade()
        migration.upgrade()
        assert connection.exec_driver_sql(f'PRAGMA index_list("{table}")').all() == before
        migration.downgrade()
        remaining = connection.exec_driver_sql(f'PRAGMA index_list("{table}")').all()
        assert {row[1] for row in remaining} == {row[1] for row in before} - {name}
        with pytest.raises(RuntimeError, match="index missing"):
            migration.downgrade()
        assert connection.exec_driver_sql(f'PRAGMA index_list("{table}")').all() == remaining
        migration.upgrade()

    async with database.engine.begin() as connection:
        await connection.run_sync(exercise)


@pytest.mark.parametrize("action", ["upgrade", "downgrade"])
async def test_all_index_shapes_prevalidated_before_first_ddl(database, monkeypatch, action):
    migration = importlib.import_module(MIGRATION)

    def exercise(connection):
        monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
        spec = migration.INDEXES[0]
        if action == "upgrade":
            connection.exec_driver_sql(f'DROP INDEX "{spec[0]}"')
        second = ("compaction_later_owned_test_index", spec[1], spec[2])
        connection.exec_driver_sql(f'CREATE VIEW "{second[0]}" AS SELECT 1 AS unrelated')
        monkeypatch.setattr(migration, "INDEXES", (spec, second))
        before = connection.exec_driver_sql(
            "SELECT type,name,sql FROM sqlite_master ORDER BY type,name"
        ).all()
        with pytest.raises(RuntimeError, match="index shape mismatch"):
            getattr(migration, action)()
        assert (
            connection.exec_driver_sql(
                "SELECT type,name,sql FROM sqlite_master ORDER BY type,name"
            ).all()
            == before
        )

    async with database.engine.begin() as connection:
        await connection.run_sync(exercise)


async def test_constraint_origin_is_not_an_owned_explicit_index(database, monkeypatch):
    migration = importlib.import_module(MIGRATION)

    def exercise(connection):
        monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
        table = "compaction_origin_test"
        connection.exec_driver_sql(
            f"CREATE TABLE {table}(fact_id INT,evidence_before INT,status TEXT,"
            "UNIQUE(fact_id,evidence_before,status))"
        )
        row = connection.exec_driver_sql(f'PRAGMA index_list("{table}")').one()
        assert row[3] == "u"
        monkeypatch.setattr(
            migration, "INDEXES", ((row[1], table, ("fact_id", "evidence_before", "status")),)
        )
        with pytest.raises(RuntimeError, match="index shape mismatch"):
            migration.downgrade()
        assert connection.exec_driver_sql(f'PRAGMA index_list("{table}")').one() == row

    async with database.engine.begin() as connection:
        await connection.run_sync(exercise)
