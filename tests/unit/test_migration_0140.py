"""Observed reply media is additive and never fabricates old historical sources."""

import sqlite3

from alembic import command
from alembic.config import Config
from tests.unit.test_migration_0119 import _seed_0118


def test_observed_reply_upgrade_preserves_existing_media_and_events(tmp_path, monkeypatch):
    path = tmp_path / "observed-reply.db"
    _seed_0118(path, monkeypatch)
    config = Config("alembic.ini")
    command.upgrade(config, "0139")
    with sqlite3.connect(path) as connection:
        event_id, conversation_id = connection.execute(
            "SELECT id,canonical_conversation_id FROM chat_events LIMIT 1"
        ).fetchone()
        connection.execute(
            "INSERT INTO conversation_media_items "
            "(source_event_id,attachment_index,conversation_id,generation,segment_index,kind,"
            "display_name,created_at,cache_status,content_sha256,cache_name) "
            "VALUES(?,0,?,1,0,'image','old.png','2026-10-18','cached',?,'0-original.png')",
            (event_id, conversation_id, "a" * 64),
        )
        media = connection.execute("SELECT * FROM conversation_media_items").fetchall()
        original_columns = [
            row[1] for row in connection.execute("PRAGMA table_info(conversation_media_items)")
        ]
        events = connection.execute("SELECT * FROM chat_events").fetchall()
    command.upgrade(config, "0140")
    with sqlite3.connect(path) as connection:
        assert (
            connection.execute(
                "SELECT " + ",".join(original_columns) + " FROM conversation_media_items"
            ).fetchall()
            == media
        )
        assert connection.execute(
            "SELECT source_snapshot_json FROM conversation_media_items"
        ).fetchall() == [(None,)]
        assert connection.execute("SELECT * FROM chat_events").fetchall() == events
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()
