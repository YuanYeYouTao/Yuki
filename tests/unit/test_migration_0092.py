"""Reply maintenance indexes preserve facts and reject incompatible owned shapes."""

import asyncio
import importlib
import sqlite3

import pytest
from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text

from qq_ai_bot.persistence.schema_guard import (
    CanonicalSchemaError,
    canonical_schema_revision,
    require_canonical_schema,
)

MIGRATION = "migrations.versions.0092_reply_maintenance_read_indexes"


@pytest.mark.parametrize("index_number", [0, 1])
@pytest.mark.parametrize("shape", ["missing", "unique", "partial", "descending", "collation"])
async def test_startup_rejects_reply_index_drift(database, index_number, shape):
    migration = importlib.import_module(MIGRATION)
    name, table, columns = migration.INDEXES[index_number]
    async with database.engine.begin() as connection:
        await connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32))"))
        await connection.execute(
            text("INSERT INTO alembic_version VALUES (:revision)"),
            {"revision": canonical_schema_revision()},
        )
        await connection.execute(text(f"DROP INDEX {name}"))
        if shape != "missing":
            unique = "UNIQUE " if shape == "unique" else ""
            predicate = " WHERE 1=1" if shape == "partial" else ""
            suffix = {"descending": " DESC", "collation": " COLLATE NOCASE"}.get(shape, "")
            keys = ",".join(columns) + suffix
            await connection.execute(
                text(f"CREATE {unique}INDEX {name} ON {table}({keys}){predicate}")
            )
    with pytest.raises(CanonicalSchemaError, match="reply maintenance index"):
        await require_canonical_schema(database.url)


async def test_reply_indexes_upgrade_downgrade_metadata_and_full_query_plans(
    database, tmp_path, monkeypatch
):
    path = tmp_path / "reply-indexes.sqlite3"
    url = f"sqlite+aiosqlite:///{path.as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    config = Config("alembic.ini")
    await asyncio.to_thread(command.upgrade, config, "0091")
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO runtime_protocol_objects VALUES ('kept', 17, 1, 0)")
        db.commit()
        before = db.execute("SELECT * FROM runtime_protocol_objects").fetchall()
        quota = db.execute("SELECT * FROM runtime_protocol_usage").fetchall()
        old_index = db.execute(
            "SELECT sql FROM sqlite_master WHERE name='ix_protocol_objects_gc'"
        ).fetchone()
    await asyncio.to_thread(command.upgrade, config, "0092")
    migration = importlib.import_module(MIGRATION)
    async with database.engine.connect() as connection:
        metadata = {
            name: await connection.scalar(
                text("SELECT sql FROM sqlite_master WHERE name=:name"), {"name": name}
            )
            for name, _table, _columns in migration.INDEXES
        }
    with sqlite3.connect(path) as db:
        for name, _table, _columns in migration.INDEXES:
            assert (
                db.execute("SELECT sql FROM sqlite_master WHERE name=?", (name,)).fetchone()[0]
                == (metadata[name])
            )
        social_plan = db.execute(
            "EXPLAIN QUERY PLAN SELECT * FROM social_operation_receipts "
            "WHERE source_conversation_id=? AND updated_at>=? "
            "ORDER BY updated_at DESC LIMIT 2048",
            ("scope", "2026-10-04"),
        ).fetchall()
        assert any(
            "SEARCH" in row[3] and "ix_social_operation_scope_updated" in row[3]
            for row in social_plan
        )
        plans = [social_plan]
        for deleting in (0, 1):
            plan = db.execute(
                "EXPLAIN QUERY PLAN SELECT sha256,byte_size,prepared_at,deleting "
                "FROM runtime_protocol_objects WHERE deleting=? AND prepared_at<? "
                "AND (prepared_at,sha256)>(?,?) AND (prepared_at,sha256)<=(?,?) "
                "ORDER BY prepared_at,sha256 LIMIT 128",
                (deleting, 100, 0, "", 99, "z"),
            ).fetchall()
            assert any(
                "SEARCH" in row[3] and "ix_protocol_objects_gc_cursor" in row[3] for row in plan
            )
            plans.append(plan)
        assert not any("TEMP B-TREE" in row[3] for plan in plans for row in plan)
    await require_canonical_schema(url)
    await asyncio.to_thread(command.downgrade, config, "0091")
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT * FROM runtime_protocol_objects").fetchall() == before
        assert db.execute("SELECT * FROM runtime_protocol_usage").fetchall() == quota
        assert (
            db.execute(
                "SELECT sql FROM sqlite_master WHERE name='ix_protocol_objects_gc'"
            ).fetchone()
            == old_index
        )
        for name, _table, _columns in migration.INDEXES:
            assert (
                db.execute("SELECT name FROM sqlite_master WHERE name=?", (name,)).fetchone()
                is None
            )
    await asyncio.to_thread(command.upgrade, config, "0092")


@pytest.mark.parametrize("action", ["upgrade", "downgrade"])
@pytest.mark.parametrize(
    "shape", ["column", "unique", "partial", "descending", "collation", "table"]
)
async def test_reply_migration_validates_all_owned_shapes_before_ddl(
    database, monkeypatch, action, shape
):
    migration = importlib.import_module(MIGRATION)

    def exercise(connection):
        monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
        name, table, columns = migration.INDEXES[0]
        connection.exec_driver_sql(f"DROP INDEX {name}")
        columns = ("source_conversation_id", "created_at") if shape == "column" else columns
        unique = "UNIQUE " if shape == "unique" else ""
        predicate = " WHERE 1=1" if shape == "partial" else ""
        suffix = {"descending": " DESC", "collation": " COLLATE NOCASE"}.get(shape, "")
        if shape == "table":
            table = "retired_receipts"
            connection.exec_driver_sql(
                f"CREATE TABLE {table}(source_conversation_id TEXT,updated_at TEXT)"
            )
        connection.exec_driver_sql(
            f"CREATE {unique}INDEX {name} ON {table}({','.join(columns)}{suffix}){predicate}"
        )
        other = migration.INDEXES[1][0]
        with pytest.raises(RuntimeError, match="index shape mismatch"):
            getattr(migration, action)()
        assert (
            connection.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE name=?", (other,)
            ).first()
            is not None
        )

    async with database.engine.begin() as connection:
        await connection.run_sync(exercise)


async def test_reply_migration_prevalidates_later_index_and_is_idempotent(database, monkeypatch):
    migration = importlib.import_module(MIGRATION)

    def exercise(connection):
        monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
        first, second = migration.INDEXES
        connection.exec_driver_sql(f"DROP INDEX {first[0]}")
        connection.exec_driver_sql(f"DROP INDEX {second[0]}")
        connection.exec_driver_sql(f"CREATE INDEX {second[0]} ON {second[1]}(prepared_at)")
        with pytest.raises(RuntimeError, match="index shape mismatch"):
            migration.upgrade()
        assert (
            connection.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE name=?", (first[0],)
            ).first()
            is None
        )
        connection.exec_driver_sql(f"DROP INDEX {second[0]}")
        migration.upgrade()
        migration.upgrade()
        migration.downgrade()
        migration.upgrade()

    async with database.engine.begin() as connection:
        await connection.run_sync(exercise)
