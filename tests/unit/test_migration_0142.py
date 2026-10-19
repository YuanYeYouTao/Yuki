"""Actual upgraded SQLite uses the step index and preserves all existing rows."""

import sqlite3

from alembic import command
from alembic.config import Config
from tests.unit.test_migration_0119 import _seed_0118


def test_step_index_upgrade_and_downgrade_preserve_facts(tmp_path, monkeypatch):
    path = tmp_path / "steps.db"
    _seed_0118(path, monkeypatch)
    config = Config("alembic.ini")
    command.upgrade(config, "0141")
    query = (
        "EXPLAIN QUERY PLAN SELECT effect_key FROM runtime_work_effects "
        + "WHERE work_id='a' AND kind='tool' ORDER BY created,effect_key LIMIT 50"
    )
    with sqlite3.connect(path) as connection:
        tables = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name != 'alembic_version'"
            )
        ]
        before = {
            table: connection.execute(f'SELECT * FROM "{table}"').fetchall() for table in tables
        }
        assert "ix_runtime_work_tool_steps" not in str(connection.execute(query).fetchall())
    command.upgrade(config, "0142")
    with sqlite3.connect(path) as connection:
        assert "ix_runtime_work_tool_steps" in str(connection.execute(query).fetchall())
        for table, rows in before.items():
            assert connection.execute(f'SELECT * FROM "{table}"').fetchall() == rows
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()
    command.downgrade(config, "0141")
    with sqlite3.connect(path) as connection:
        assert "ix_runtime_work_tool_steps" not in str(connection.execute(query).fetchall())
