"""New credentials retain old identity and execution facts during SQLite rebuild."""

import sqlite3
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from tests.unit.test_migration_0119 import _seed_0118


def test_0121_retains_binding_owners_and_history_and_reinstalls_route_guards(tmp_path, monkeypatch):
    path = tmp_path / "credentials.db"
    _seed_0118(path, monkeypatch)
    config = Config("alembic.ini")
    command.upgrade(config, "0120")
    with sqlite3.connect(path) as connection:
        original = connection.execute("SELECT * FROM identity_bindings ORDER BY id").fetchall()
        history = connection.execute("SELECT * FROM chat_events ORDER BY id").fetchall()
        work = connection.execute("SELECT * FROM runtime_work ORDER BY id").fetchall()
        private = connection.execute(
            "SELECT id,person_id,identity_binding_id FROM canonical_conversations ORDER BY id"
        ).fetchall()
    command.upgrade(config, "0121")
    with sqlite3.connect(path) as connection:
        assert [
            tuple(row[:-1])
            for row in connection.execute("SELECT * FROM identity_bindings ORDER BY id")
        ] == original
        assert connection.execute(
            "SELECT DISTINCT history_starts_at FROM identity_bindings"
        ).fetchall() == [(None,)]
        assert connection.execute("SELECT * FROM chat_events ORDER BY id").fetchall() == history
        assert connection.execute("SELECT * FROM runtime_work ORDER BY id").fetchall() == work
        assert (
            connection.execute(
                "SELECT id,person_id,identity_binding_id FROM canonical_conversations ORDER BY id"
            ).fetchall()
            == private
        )
        assert connection.execute(
            "SELECT count(*) FROM presences WHERE platform='app'"
        ).fetchone() == (1,)
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert {"client_devices", "client_link_codes"} <= tables
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()
        old, target = original[:2]
        now = "2026-10-15 00:00:00"
        presence = connection.execute("SELECT id FROM presences WHERE platform='qq'").fetchone()[0]
        connection.execute(
            "INSERT INTO person_active_routes VALUES(?,?,?,1,0,1,?,?)",
            (old[1], old[0], presence, now, now),
        )
        with pytest.raises(sqlite3.IntegrityError, match="break person active route"):
            connection.execute(
                "UPDATE identity_bindings SET person_id=? WHERE id=?", (target[1], old[0])
            )
        connection.execute(
            "UPDATE identity_bindings SET status='transferred' WHERE id=?", (old[0],)
        )
        connection.execute(
            "INSERT INTO identity_bindings(id,person_id,platform,external_account_id,"
            "display_name,status,revision,"
            "first_seen_at,last_seen_at,created_at,updated_at,history_starts_at) "
            "VALUES(?,?,'qq',?,'','active',1,?,?,?,?,?)",
            (str(uuid4()), target[1], old[3], now, now, now, now, now),
        )
        assert connection.execute(
            "SELECT person_id FROM identity_bindings WHERE id=?", (old[0],)
        ).fetchone() == (old[1],)
