"""Existing usage rows remain explicitly unmeasured after adding HTTP counts."""

import sqlite3

from alembic import command
from alembic.config import Config


def test_physical_request_columns_preserve_legacy_unknown_and_downgrade(tmp_path, monkeypatch):
    path = tmp_path / "model-requests.sqlite3"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{path.as_posix()}")
    config = Config("alembic.ini")
    command.upgrade(config, "0078")
    with sqlite3.connect(path) as db:
        db.execute(
            "INSERT INTO model_invocations "
            "(task, profile_id, provider, model, success, latency_seconds, created_at) "
            "VALUES ('chat_agent', 'old', 'fixture', 'offline', 1, 0.1, '2026-09-29')"
        )
    command.upgrade(config, "0079")
    with sqlite3.connect(path) as db:
        assert db.execute(
            "SELECT physical_request_count, unknown_usage_request_count, "
            "native_search_requested FROM model_invocations WHERE profile_id='old'"
        ).fetchone() == (None, None, None)
        assert db.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    command.downgrade(config, "0078")
    with sqlite3.connect(path) as db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(model_invocations)")}
        assert "physical_request_count" not in columns
        assert db.execute("SELECT profile_id FROM model_invocations").fetchone() == ("old",)
