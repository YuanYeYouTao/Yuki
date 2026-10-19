"""Upgrade the preceding real SQLite schema without touching original records."""

import sqlite3

from alembic import command
from alembic.config import Config
from tests.unit.test_migration_0119 import _seed_0118


def test_executor_upgrade_downgrade_preserves_original_tables(tmp_path, monkeypatch):
    path = tmp_path / "executors.db"
    _seed_0118(path, monkeypatch)
    config = Config("alembic.ini")
    command.upgrade(config, "0145")
    with sqlite3.connect(path) as connection:
        names = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name != 'alembic_version'"
            )
        ]
        before = {name: connection.execute(f'SELECT * FROM "{name}"').fetchall() for name in names}
    command.upgrade(config, "0146")
    with sqlite3.connect(path) as connection:
        for name, rows in before.items():
            assert connection.execute(f'SELECT * FROM "{name}"').fetchall() == rows
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()
        assert connection.execute("SELECT COUNT(*) FROM client_executors").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM executor_operations").fetchone() == (0,)
        plan = connection.execute(
            "EXPLAIN QUERY PLAN SELECT op_id FROM executor_operations "
            "WHERE executor_id='x' ORDER BY created_at DESC,op_id DESC LIMIT 50"
        ).fetchall()
        assert "ix_executor_operations_history" in str(plan)
    command.downgrade(config, "0145")
    with sqlite3.connect(path) as connection:
        for name, rows in before.items():
            assert connection.execute(f'SELECT * FROM "{name}"').fetchall() == rows
