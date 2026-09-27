"""Operator indexes change no Work facts and are reversible on deployed schema."""

import sqlite3

from alembic import command
from alembic.config import Config


def test_work_indexes_upgrade_and_downgrade_without_row_changes(tmp_path, monkeypatch):
    path = tmp_path / "work-indexes.sqlite3"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{path.as_posix()}")
    config = Config("alembic.ini")
    command.upgrade(config, "0075")
    with sqlite3.connect(path) as db:
        before = {
            table: db.execute(f"SELECT * FROM {table}").fetchall()
            for table in (
                "runtime_work",
                "runtime_work_inputs",
                "runtime_work_effects",
                "runtime_subagents",
                "runtime_work_waits",
            )
        }
    command.upgrade(config, "0076")
    with sqlite3.connect(path) as db:
        names = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        assert {
            "ix_work_inputs_work_id",
            "ix_work_effects_work_updated",
            "ix_work_children_root",
            "ix_work_waits_work_created",
        } <= names
        for table, rows in before.items():
            assert db.execute(f"SELECT * FROM {table}").fetchall() == rows
        explain = db.execute(
            "EXPLAIN QUERY PLAN SELECT effect_key FROM runtime_work_effects "
            "WHERE work_id=? ORDER BY updated DESC, effect_key DESC LIMIT 21",
            ("original-work",),
        ).fetchall()
        assert "ix_work_effects_work_updated" in repr(explain)
        assert db.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    command.downgrade(config, "0075")
    with sqlite3.connect(path) as db:
        assert (
            db.execute(
                "SELECT name FROM sqlite_master WHERE name='ix_work_effects_work_updated'"
            ).fetchall()
            == []
        )
