"""The deployment bridge and fresh ORM agree on actorless Memory provenance."""

import sqlite3
from contextlib import closing

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine
from tests.unit.test_migration_0066 import _schema

from qq_ai_bot.persistence.metadata import Base


def test_self_memory_migration_matches_runtime_schema(tmp_path, monkeypatch):
    path = tmp_path / "memory.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{path.as_posix()}")
    config = Config("alembic.ini")
    command.upgrade(config, "0066")
    with closing(sqlite3.connect(path)) as connection:
        assert "initiative_run_id" not in {
            row[1] for row in connection.execute("PRAGMA table_info(memory_tool_receipts)")
        }
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
