"""The deployment bridge and fresh ORM agree on actorless Memory provenance."""

import importlib
import sqlite3
from contextlib import closing

import pytest
from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine
from tests.unit.test_migration_0066 import _schema

from qq_ai_bot.persistence.metadata import Base


@pytest.mark.parametrize("retained_columns", [False, True])
def test_self_memory_migration_matches_runtime_schema(tmp_path, monkeypatch, retained_columns):
    path = tmp_path / "memory.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{path.as_posix()}")
    config = Config("alembic.ini")
    command.upgrade(config, "0066")
    with closing(sqlite3.connect(path)) as connection:
        assert "initiative_run_id" not in {
            row[1] for row in connection.execute("PRAGMA table_info(memory_tool_receipts)")
        }
        if retained_columns:
            # A partially retained schema must acquire the missing constraints,
            # not duplicate its columns or silently skip the remaining upgrade.
            for name, size in (
                ("initiative_run_id", 36),
                ("tool_call_id", 255),
                ("execution_id", 255),
                ("source_call_key", 64),
            ):
                connection.execute(
                    f"ALTER TABLE memory_tool_receipts ADD COLUMN {name} VARCHAR({size})"
                )
            connection.execute(
                "ALTER TABLE memory_mutation_receipts ADD COLUMN initiative_run_id VARCHAR(36)"
            )
    command.upgrade(config, "0067")
    command.downgrade(config, "0066")
    command.upgrade(config, "0067")
    runtime = create_engine("sqlite:///:memory:")
    deployed = create_engine(f"sqlite:///{path.as_posix()}")
    try:
        with runtime.begin() as model_connection, deployed.connect() as migration_connection:
            Base.metadata.create_all(model_connection)
            for name in (
                "memory_tool_receipts",
                "memory_mutation_receipts",
                "memory_initiative_reflection_cursors",
                "memory_initiative_reflection_windows",
            ):
                migrated = _schema(migration_connection, name)
                fresh = _schema(model_connection, name)
                # ALTER appends nullable columns; position is not part of the contract.
                migrated["columns"].sort()
                fresh["columns"].sort()
                assert migrated == fresh
            assert migration_connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
    finally:
        runtime.dispose()
        deployed.dispose()


@pytest.mark.asyncio
async def test_self_memory_replayed_upgrade_preserves_orm_receipts_and_watermarks(
    database, monkeypatch
):
    from tests.unit.test_self_initiative_memory_quality import reflection_fact

    from qq_ai_bot.memory.self_reflection.repository import SelfReflectionRepository

    _, _, _, batch, _ = await reflection_fact(database)
    await SelfReflectionRepository(database).complete(batch, proposals=1, committed=1)
    migration = importlib.import_module("migrations.versions.0067_self_initiative_memory")
    tables = (
        "memory_tool_receipts",
        "memory_mutation_receipts",
        "memory_initiative_reflection_cursors",
        "memory_initiative_reflection_windows",
    )

    def replay(connection):
        before_rows = {
            name: connection.exec_driver_sql(f"SELECT * FROM {name} ORDER BY 1").all()
            for name in tables
        }
        assert before_rows["memory_tool_receipts"]
        assert before_rows["memory_initiative_reflection_cursors"]
        assert before_rows["memory_initiative_reflection_windows"]
        before_schema = {name: _schema(connection, name) for name in tables}
        monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
        migration.upgrade()
        migration.upgrade()
        migration.downgrade()
        migration.upgrade()
        assert before_schema == {name: _schema(connection, name) for name in tables}
        assert before_rows == {
            name: connection.exec_driver_sql(f"SELECT * FROM {name} ORDER BY 1").all()
            for name in tables
        }
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []

    async with database.engine.begin() as connection:
        await connection.run_sync(replay)
