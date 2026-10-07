"""Cleanup indexes bound discovery without changing any cached source rows."""

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

INDEXES = (
    ("ix_media_analyses_expires_at", "media_analyses", "expires_at"),
    ("ix_web_search_runs_created_at", "web_search_runs", "created_at"),
)


async def test_cleanup_index_real_round_trip_matches_metadata_and_covers_discovery(
    database, tmp_path, monkeypatch
):
    path = tmp_path / "cleanup-index.sqlite3"
    url = f"sqlite+aiosqlite:///{path.as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    config = Config("alembic.ini")
    await asyncio.to_thread(command.upgrade, config, "0083")
    with sqlite3.connect(path) as db:
        db.execute(
            "INSERT INTO media_analyses (content_hash,analysis_mode,question_hash,provider,model,"
            "prompt_version,observation_json,segment_index,created_at,expires_at) "
            "VALUES (?,'general','','test','test','test','{}',0,?,?)",
            ("a" * 64, "2026-01-01", "2026-01-02"),
        )
        db.execute(
            "INSERT INTO web_search_runs (conversation_key,trigger_message_id,query,provider,"
            "created_at,partial_failure) VALUES ('test','','verified','test',?,0)",
            ("2026-01-01",),
        )
        before = {table: db.execute(f"SELECT * FROM {table}").fetchall() for _, table, _ in INDEXES}
    # Index rollback belongs to the reversible pre-retirement chain. Current
    # startup validation still runs after the final real upgrade below.
    await asyncio.to_thread(command.upgrade, config, "0096")
    async with database.engine.connect() as connection:
        metadata_sql = {
            name: await connection.scalar(
                text("SELECT sql FROM sqlite_master WHERE type='index' AND name=:name"),
                {"name": name},
            )
            for name, _table, _column in INDEXES
        }
    with sqlite3.connect(path) as db:
        for name, table, column in INDEXES:
            assert (
                db.execute(
                    "SELECT sql FROM sqlite_master WHERE type='index' AND name=?", (name,)
                ).fetchone()[0]
                == metadata_sql[name]
            )
            comparison = "<=" if column == "expires_at" else "<"
            plan = db.execute(
                f"EXPLAIN QUERY PLAN SELECT id FROM {table} WHERE {column}{comparison}? "
                f"ORDER BY {column},id LIMIT 128",
                ("2026-10-01",),
            ).fetchall()
            assert any(f"SEARCH {table} USING COVERING INDEX {name}" in row[3] for row in plan)
            assert not any("TEMP B-TREE" in row[3] for row in plan)
        work_plan = db.execute(
            "EXPLAIN QUERY PLAN SELECT runtime_work.id FROM runtime_work "
            "JOIN canonical_conversations c ON c.id=runtime_work.conversation_id "
            "WHERE runtime_work.state='waiting_external' AND runtime_work.generation=c.generation "
            "AND json_extract(runtime_work.checkpoint_json,'$.context_rollup.coverage') "
            "IS NOT NULL "
            "ORDER BY runtime_work.updated LIMIT 32"
        ).fetchall()
        assert any(
            "SEARCH runtime_work USING INDEX ix_runtime_work_state_updated" in row[3]
            for row in work_plan
        )
        assert not any("TEMP B-TREE" in row[3] for row in work_plan)
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert db.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    await asyncio.to_thread(command.downgrade, config, "0083")
    with sqlite3.connect(path) as db:
        assert (
            db.execute(
                "SELECT name FROM sqlite_master WHERE name='ix_runtime_work_state_updated'"
            ).fetchone()
            is None
        )
        for name, table, _column in INDEXES:
            assert (
                db.execute(
                    "SELECT sql FROM sqlite_master WHERE type='index' AND name=?", (name,)
                ).fetchone()
                is None
            )
            assert db.execute(f"SELECT * FROM {table}").fetchall() == before[table]
    await asyncio.to_thread(command.upgrade, config, "head")
    await require_canonical_schema(url)


@pytest.mark.parametrize("name,table,column", INDEXES)
@pytest.mark.parametrize(
    "shape", ["missing", "wrong_table", "wrong_column", "partial", "unique", "descending"]
)
async def test_cleanup_index_retained_drift_fails_migration_and_startup(
    database, monkeypatch, name, table, column, shape
):
    migration = importlib.import_module("migrations.versions.0084_cache_cleanup_indexes")

    def exercise(connection):
        monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
        connection.exec_driver_sql(f'DROP INDEX "{name}"')
        if shape != "missing":
            target = table
            if shape == "wrong_table":
                target = table + "_retired"
                connection.exec_driver_sql(f'CREATE TABLE "{target}" ("{column}" TEXT)')
            key = "created_at" if shape == "wrong_column" and column == "expires_at" else column
            if shape == "wrong_column" and column == "created_at":
                key = "query"
            unique = "UNIQUE " if shape == "unique" else ""
            order = " DESC" if shape == "descending" else ""
            predicate = f' WHERE "{column}" IS NOT NULL' if shape == "partial" else ""
            connection.exec_driver_sql(
                f'CREATE {unique}INDEX "{name}" ON "{target}" ("{key}"{order}){predicate}'
            )
            with pytest.raises(RuntimeError, match="index shape mismatch"):
                migration.upgrade()
        with pytest.raises(RuntimeError, match=r"index (shape mismatch|is missing)"):
            migration.downgrade()
        connection.exec_driver_sql("CREATE TABLE alembic_version (version_num VARCHAR(32))")
        connection.exec_driver_sql(
            "INSERT INTO alembic_version VALUES (?)", (canonical_schema_revision(),)
        )

    async with database.engine.begin() as connection:
        await connection.run_sync(exercise)
    with pytest.raises(CanonicalSchemaError, match="cache cleanup index"):
        await require_canonical_schema(database.url)


async def test_cleanup_index_upgrade_accepts_exact_current_metadata(database, monkeypatch):
    migration = importlib.import_module("migrations.versions.0084_cache_cleanup_indexes")

    def exercise(connection):
        monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
        migration.upgrade()
        migration.downgrade()
        migration.upgrade()

    async with database.engine.begin() as connection:
        await connection.run_sync(exercise)


@pytest.mark.parametrize("keys", ["updated,state", "state COLLATE NOCASE,updated"])
async def test_work_wait_discovery_index_rejects_changed_order_or_collation(
    database, monkeypatch, keys
):
    migration = importlib.import_module("migrations.versions.0084_cache_cleanup_indexes")

    def exercise(connection):
        monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
        connection.exec_driver_sql("DROP INDEX ix_runtime_work_state_updated")
        connection.exec_driver_sql(
            f"CREATE INDEX ix_runtime_work_state_updated ON runtime_work({keys})"
        )
        with pytest.raises(RuntimeError, match="index shape mismatch"):
            migration.upgrade()
        connection.exec_driver_sql("CREATE TABLE alembic_version (version_num VARCHAR(32))")
        connection.exec_driver_sql(
            "INSERT INTO alembic_version VALUES (?)", (canonical_schema_revision(),)
        )

    async with database.engine.begin() as connection:
        await connection.run_sync(exercise)
    with pytest.raises(CanonicalSchemaError, match="cache cleanup index"):
        await require_canonical_schema(database.url)
