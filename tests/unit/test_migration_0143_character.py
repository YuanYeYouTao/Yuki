"""Upgrading existing data adds identity without changing original facts."""

import sqlite3

from alembic import command
from alembic.config import Config
from tests.unit.test_migration_0119 import _seed_0118


def test_character_upgrade_preserves_every_existing_fact_and_singleton(tmp_path, monkeypatch):
    path = tmp_path / "characters.db"
    _seed_0118(path, monkeypatch)
    config = Config("alembic.ini")
    command.upgrade(config, "0142")
    with sqlite3.connect(path) as connection:
        tables = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name != 'alembic_version'"
            )
        ]
        facts = {
            table: connection.execute(f'SELECT * FROM "{table}"').fetchall() for table in tables
        }
    command.upgrade(config, "0143")
    with sqlite3.connect(path) as connection:
        identity, name = connection.execute(
            "SELECT id,display_name FROM character_profile"
        ).fetchone()
        assert name == "Yuki"
        for table, rows in facts.items():
            assert connection.execute(f'SELECT * FROM "{table}"').fetchall() == rows
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()
    command.upgrade(config, "head")
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT id FROM character_profile").fetchone()[0] == identity
    command.downgrade(config, "0142")
    with sqlite3.connect(path) as connection:
        for table, rows in facts.items():
            assert connection.execute(f'SELECT * FROM "{table}"').fetchall() == rows
