"""Partial execution indexes keep operator reads bounded as diagnostics grow."""

import sqlite3

from alembic import command
from alembic.config import Config


def test_execution_read_indexes_upgrade_and_downgrade(tmp_path, monkeypatch):
    path = tmp_path / "execution-indexes.sqlite3"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{path.as_posix()}")
    config = Config("alembic.ini")
    command.upgrade(config, "0076")
    command.upgrade(config, "0077")
    with sqlite3.connect(path) as db:
        indexes = {
            row[0]: row[1]
            for row in db.execute(
                "SELECT name, sql FROM sqlite_master WHERE type='index' "
                "AND name IN ('ix_execution_trace_roots', 'ix_execution_trace_source_event')"
            )
        }
        assert len(indexes) == 2
        assert (
            "WHERE kind IN ('chat_processing_start', 'turn_start')"
            in indexes["ix_execution_trace_roots"]
        )
        assert "WHERE source_event_id IS NOT NULL" in indexes["ix_execution_trace_source_event"]
        root_plan = db.execute(
            "EXPLAIN QUERY PLAN SELECT turn_id FROM execution_trace_entries "
            "WHERE conversation_id=? AND expires_at>? "
            "AND kind IN ('chat_processing_start', 'turn_start') "
            "ORDER BY id DESC LIMIT 6",
            ("conversation", "2026-01-01"),
        ).fetchall()
        source_plan = db.execute(
            "EXPLAIN QUERY PLAN SELECT turn_id FROM execution_trace_entries "
            "WHERE source_event_id=? AND source_event_id IS NOT NULL "
            "AND conversation_id=? AND expires_at>? GROUP BY turn_id, conversation_id",
            (42, "conversation", "2026-01-01"),
        ).fetchall()
        assert "ix_execution_trace_roots" in repr(root_plan)
        assert "ix_execution_trace_source_event" in repr(source_plan)
        assert db.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    command.downgrade(config, "0076")
    with sqlite3.connect(path) as db:
        assert not db.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND name IN ('ix_execution_trace_roots', 'ix_execution_trace_source_event')"
        ).fetchall()
