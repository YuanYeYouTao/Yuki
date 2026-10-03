"""Frozen maintenance index shapes and plans match fresh metadata and survive round trips."""

import asyncio
import importlib
import sqlite3

import pytest
from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text

MIGRATION = "migrations.versions.0090_memory_maintenance_read_indexes"


async def test_maintenance_indexes_real_upgrade_downgrade_and_query_plans(
    database, tmp_path, monkeypatch
):
    path = tmp_path / "maintenance-indexes.sqlite3"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{path.as_posix()}")
    config = Config("alembic.ini")
    await asyncio.to_thread(command.upgrade, config, "0089")
    await asyncio.to_thread(command.upgrade, config, "0090")
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
                == metadata[name]
            )
        recovery_plan = db.execute(
            "EXPLAIN QUERY PLAN SELECT id, claimed_at, attempts FROM memory_reflection_jobs "
            "WHERE status='processing' AND claimed_at <= ? ORDER BY claimed_at,id LIMIT 100",
            ("2026-10-03",),
        ).fetchall()
        dream_plan = db.execute(
            "EXPLAIN QUERY PLAN SELECT id FROM memory_dream_clusters "
            "WHERE status='processing' ORDER BY id LIMIT 128"
        ).fetchall()
        assert any("ix_memory_reflection_jobs_status_claimed" in row[3] for row in recovery_plan)
        assert any("ix_memory_dream_clusters_status_id" in row[3] for row in dream_plan)
        assert not any("TEMP B-TREE" in row[3] for row in (*recovery_plan, *dream_plan))
        before = {
            table: db.execute(f"SELECT * FROM {table}").fetchall()
            for table in (
                "memory_facts",
                "memory_reflection_jobs",
                "memory_dream_clusters",
                "memory_dream_runs",
            )
        }
    await asyncio.to_thread(command.downgrade, config, "0089")
    with sqlite3.connect(path) as db:
        for name, _table, _columns in migration.INDEXES:
            assert (
                db.execute("SELECT name FROM sqlite_master WHERE name=?", (name,)).fetchone()
                is None
            )
        assert before == {
            table: db.execute(f"SELECT * FROM {table}").fetchall() for table in before
        }
    await asyncio.to_thread(command.upgrade, config, "0090")


@pytest.mark.parametrize("action", ["upgrade", "downgrade"])
@pytest.mark.parametrize(
    "shape", ["metadata", "column", "unique", "partial", "descending", "table"]
)
async def test_maintenance_migration_validates_owned_shape_before_any_ddl(
    database, monkeypatch, action, shape
):
    migration = importlib.import_module(MIGRATION)

    def exercise(connection):
        monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
        name, table, columns = migration.INDEXES[0]
        if shape != "metadata":
            connection.exec_driver_sql(f"DROP INDEX {name}")
            columns = ("status", "next_attempt_at") if shape == "column" else columns
            unique = "UNIQUE " if shape == "unique" else ""
            predicate = " WHERE claimed_at IS NOT NULL" if shape == "partial" else ""
            keys = ",".join(columns) + (" DESC" if shape == "descending" else "")
            if shape == "table":
                table = "retired_jobs"
                connection.exec_driver_sql(
                    f"CREATE TABLE {table}(status TEXT,claimed_at TEXT,id INTEGER)"
                )
            connection.exec_driver_sql(f"CREATE {unique}INDEX {name} ON {table}({keys}){predicate}")
            with pytest.raises(RuntimeError, match="index shape mismatch"):
                getattr(migration, action)()
            # Validation of one index cannot drop or create the other owned index.
            other = migration.INDEXES[1][0]
            assert (
                connection.exec_driver_sql(
                    "SELECT name FROM sqlite_master WHERE name=?", (other,)
                ).first()
                is not None
            )
        else:
            migration.upgrade()
            migration.downgrade()
            migration.upgrade()

    async with database.engine.begin() as connection:
        await connection.run_sync(exercise)
