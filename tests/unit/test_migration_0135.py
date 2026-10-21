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
        plan = connection.execute(
            "EXPLAIN QUERY PLAN SELECT sender_group_card,sender_nickname FROM chat_events "
            "WHERE canonical_conversation_id=? AND sender_user_id=? AND occurred_at<=? "
            "AND suppression_status='keeper' ORDER BY occurred_at DESC,id DESC LIMIT 1",
            ("original-conversation", "original-account", "2026-10-18 12:00:00"),
        ).fetchall()
        assert any("ix_chat_events_conversation_sender_time" in row[3] for row in plan), plan
        assert not any("TEMP B-TREE" in row[3] or "SCAN chat_events" in row[3] for row in plan), (
            plan
        )
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
