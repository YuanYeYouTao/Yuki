"""Actual migration preserves rows and indexes bounded event-to-Work lookup."""

import sqlite3

from alembic import command
from alembic.config import Config
from tests.unit.test_migration_0119 import _seed_0118


def test_event_work_index_upgrade_and_downgrade_preserve_facts(tmp_path, monkeypatch):
    path = tmp_path / "event-work.db"
    _seed_0118(path, monkeypatch)
    config = Config("alembic.ini")
    command.upgrade(config, "0142")
    query = (
        "EXPLAIN QUERY PLAN SELECT id FROM runtime_work WHERE conversation_id='original' "
        "AND parent_work_id IS NULL AND json_extract(source_json, '$.trigger_event_id') IN (1,2) "
        "ORDER BY created,id"
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
        assert "ix_runtime_work_client_trigger" not in str(connection.execute(query).fetchall())
    command.upgrade(config, "0143")
    with sqlite3.connect(path) as connection:
        assert "ix_runtime_work_client_trigger" in str(connection.execute(query).fetchall())
        for table, rows in before.items():
            assert connection.execute(f'SELECT * FROM "{table}"').fetchall() == rows
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()
    command.downgrade(config, "0142")
    with sqlite3.connect(path) as connection:
        assert "ix_runtime_work_client_trigger" not in str(connection.execute(query).fetchall())
