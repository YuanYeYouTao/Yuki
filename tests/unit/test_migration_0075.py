"""Delivery diagnostics never rewrite old traces or reuse erased trace IDs."""

import sqlite3

from alembic import command
from alembic.config import Config


def test_delivery_upgrade_preserves_erased_sequence_and_existing_evidence(tmp_path, monkeypatch):
    path = tmp_path / "delivery-migration.sqlite3"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{path.as_posix()}")
    config = Config("alembic.ini")
    command.upgrade(config, "0074")
    sql = (
        "INSERT INTO execution_trace_entries (turn_id, operation_id, kind, payload_status, "
        "payload_bytes, created_at, expires_at) "
        "VALUES ('original', 'step', 'turn_start', 'omitted_size', 2048, "
        "'2026-09-27', '2026-10-27')"
    )
    with sqlite3.connect(path) as db:
        db.execute(sql)
        db.execute(sql)
        db.execute("DELETE FROM execution_trace_entries WHERE id=2")
        before = db.execute("SELECT * FROM execution_trace_entries").fetchall()
        original_sql = db.execute(
            "SELECT sql FROM sqlite_master WHERE name='execution_trace_entries'"
        ).fetchone()[0]
    command.upgrade(config, "0075")
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT * FROM execution_trace_entries").fetchall() == [
            (*before[0], None)
        ]
        new_id = db.execute(sql).lastrowid
        assert new_id == 3
        assert "AUTOINCREMENT" in original_sql
        assert (
            "AUTOINCREMENT"
            in db.execute(
                "SELECT sql FROM sqlite_master WHERE name='execution_trace_entries'"
            ).fetchone()[0]
        )
        assert db.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    command.downgrade(config, "0074")
    with sqlite3.connect(path) as db:
        assert "delivered_event_id" not in {
            row[1] for row in db.execute("PRAGMA table_info(execution_trace_entries)")
        }
        assert db.execute("SELECT count(*) FROM execution_trace_entries").fetchone() == (2,)
