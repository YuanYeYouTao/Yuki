"""Mention name lookup uses an exact local index without rewriting original facts."""

import asyncio
import sqlite3

from alembic import command
from alembic.config import Config
from tests.unit.test_migration_0119 import _seed_0118

from yuki.persistence.schema_guard import require_canonical_schema


def test_mention_name_index_preserves_events_and_receipts(tmp_path, monkeypatch):
    path = tmp_path / "mention-names.db"
    url, _identities = _seed_0118(path, monkeypatch)
    config = Config("alembic.ini")
    command.upgrade(config, "0134")
    with sqlite3.connect(path) as connection:
        before = {
            table: connection.execute(f'SELECT * FROM "{table}" ORDER BY rowid').fetchall()
            for table in ("chat_events", "canonical_event_receipts", "persons", "presences")
        }
    command.upgrade(config, "0135")
    with sqlite3.connect(path) as connection:
        for table, rows in before.items():
            assert connection.execute(f'SELECT * FROM "{table}" ORDER BY rowid').fetchall() == rows
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()
    command.downgrade(config, "0134")
    with sqlite3.connect(path) as connection:
        assert "ix_chat_events_conversation_sender_time" not in {
            row[1] for row in connection.execute("PRAGMA index_list(chat_events)")
        }
        for table, rows in before.items():
            assert connection.execute(f'SELECT * FROM "{table}" ORDER BY rowid').fetchall() == rows
    # Validate today's runtime only after the frozen migration's own downgrade.
    command.upgrade(config, "head")
    asyncio.run(require_canonical_schema(url))
