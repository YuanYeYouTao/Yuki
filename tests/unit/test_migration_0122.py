"""Entry migration preserves existing history, execution and frozen aliases."""

import sqlite3

from alembic import command
from alembic.config import Config
from tests.unit.test_migration_0119 import _seed_0118


def test_entry_upgrade_preserves_original_facts(tmp_path, monkeypatch):
    path = tmp_path / "entries.db"
    _seed_0118(path, monkeypatch)
    config = Config("alembic.ini")
    command.upgrade(config, "0121")
    tables = ("chat_events", "runtime_work", "conversation_legacy_aliases")
    with sqlite3.connect(path) as connection:
        originals = {
            table: connection.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()
            for table in tables
        }
        conversations = connection.execute(
            "SELECT id,person_id,space_id,generation,primary_alias_id "
            "FROM canonical_conversations ORDER BY id"
        ).fetchall()
    command.upgrade(config, "0122")
    with sqlite3.connect(path) as connection:
        for table, rows in originals.items():
            assert connection.execute(f"SELECT * FROM {table} ORDER BY id").fetchall() == rows
        assert (
            connection.execute(
                "SELECT id,person_id,space_id,generation,primary_alias_id "
                "FROM canonical_conversations ORDER BY id"
            ).fetchall()
            == conversations
        )
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()
