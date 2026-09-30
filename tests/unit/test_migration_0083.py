"""Receipt reference index matches fresh metadata and fails closed on retained drift."""

import asyncio
import importlib
import sqlite3

import pytest
from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from tests.unit.test_memory_maintenance_boundaries import ordinary_receipts

from qq_ai_bot.memory.models import MemoryEvidenceCreate, MemoryFactCreate
from qq_ai_bot.memory.repository import MemoryFactRepository
from qq_ai_bot.memory.service import MemoryFactService
from qq_ai_bot.persistence.schema_guard import CanonicalSchemaError, require_canonical_schema

INDEX = "ix_memory_evidence_tool_receipt"


async def test_receipt_index_real_upgrade_downgrade_matches_metadata_and_query_plan(
    database, tmp_path, monkeypatch
):
    path = tmp_path / "receipt-index.sqlite3"
    url = f"sqlite+aiosqlite:///{path.as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    config = Config("alembic.ini")
    await asyncio.to_thread(command.upgrade, config, "0082")
    with sqlite3.connect(path) as db:
        before_events = db.execute("SELECT * FROM chat_events").fetchall()
        before_receipts = db.execute("SELECT * FROM memory_tool_receipts").fetchall()
        before_evidence = db.execute("SELECT * FROM memory_evidence").fetchall()
    await asyncio.to_thread(command.upgrade, config, "0083")
    await require_canonical_schema(url)
    async with database.engine.connect() as connection:
        metadata_sql = await connection.scalar(
            text("SELECT sql FROM sqlite_master WHERE type='index' AND name=:name"), {"name": INDEX}
        )
    with sqlite3.connect(path) as db:
        migrated_sql = db.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND name=?", (INDEX,)
        ).fetchone()[0]
        assert migrated_sql == metadata_sql
        assert [row[2] for row in db.execute(f"PRAGMA index_info('{INDEX}')")] == [
            "tool_receipt_id"
        ]
        plan = db.execute(
            "EXPLAIN QUERY PLAN SELECT id FROM memory_evidence WHERE tool_receipt_id=?",
            (1,),
        ).fetchall()
        assert any(f"USING COVERING INDEX {INDEX}" in row[3] for row in plan)
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert db.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    await asyncio.to_thread(command.downgrade, config, "0082")
    with sqlite3.connect(path) as db:
        assert (
            db.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND name=?", (INDEX,)
            ).fetchone()
            is None
        )
        assert db.execute("SELECT * FROM chat_events").fetchall() == before_events
        assert db.execute("SELECT * FROM memory_tool_receipts").fetchall() == before_receipts
        assert db.execute("SELECT * FROM memory_evidence").fetchall() == before_evidence
    await asyncio.to_thread(command.upgrade, config, "0083")
    with sqlite3.connect(path) as db:
        db.execute(f"DROP INDEX {INDEX}")
        db.execute(f"CREATE INDEX {INDEX} ON memory_evidence(event_id)")
    with pytest.raises(CanonicalSchemaError, match="receipt reference index"):
        await require_canonical_schema(url)


@pytest.mark.parametrize(
    "shape", ["metadata", "wrong_table", "wrong_column", "wrong_predicate", "unique"]
)
async def test_receipt_index_upgrade_checks_retained_index_shape(database, monkeypatch, shape):
    migration = importlib.import_module("migrations.versions.0083_memory_receipt_reference_index")

    def exercise(connection):
        monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
        if shape != "metadata":
            connection.exec_driver_sql(f"DROP INDEX {INDEX}")
            table = "memory_evidence"
            if shape == "wrong_table":
                table = "memory_evidence_retired"
                connection.exec_driver_sql(f"CREATE TABLE {table} (tool_receipt_id INTEGER)")
            column = "event_id" if shape == "wrong_column" else "tool_receipt_id"
            predicate = (
                "tool_receipt_id > 0"
                if shape == "wrong_predicate"
                else ("tool_receipt_id IS NOT NULL")
            )
            unique = "UNIQUE " if shape == "unique" else ""
            connection.exec_driver_sql(
                f"CREATE {unique}INDEX {INDEX} ON {table}({column}) WHERE {predicate}"
            )
            with pytest.raises(RuntimeError, match="index shape mismatch"):
                migration.upgrade()
        else:
            migration.upgrade()

    async with database.engine.begin() as connection:
        await connection.run_sync(exercise)


async def test_receipt_index_round_trip_preserves_real_source_receipt_and_evidence(
    database, monkeypatch
):
    (receipt_id,) = await ordinary_receipts(database, 1)
    facts = MemoryFactService(MemoryFactRepository(database))
    await facts.remember(
        MemoryFactCreate(
            scope_type="self",
            visibility_type="global",
            memory_key="migration:receipt",
            category="test",
            content="verified source",
            source_type="automatic",
        ),
        evidence=MemoryEvidenceCreate(
            tool_receipt_id=receipt_id,
            source_speaker_user_id="1001",
            relation="confirmation",
            authority="self_report",
            excerpt="verified",
        ),
    )
    migration = importlib.import_module("migrations.versions.0083_memory_receipt_reference_index")

    def exercise(connection):
        monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
        tables = ("chat_events", "memory_tool_receipts", "memory_evidence")
        before = {
            table: connection.exec_driver_sql(f"SELECT * FROM {table}").fetchall()
            for table in tables
        }
        assert all(before.values())
        migration.downgrade()
        migration.upgrade()
        assert {
            table: connection.exec_driver_sql(f"SELECT * FROM {table}").fetchall()
            for table in tables
        } == before

    async with database.engine.begin() as connection:
        await connection.run_sync(exercise)
