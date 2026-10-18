"""Removing a read heartbeat preserves real device identity and revocation."""

import sqlite3
from uuid import uuid4

from alembic import command
from alembic.config import Config
from tests.unit.test_migration_0119 import _seed_0118


def test_device_heartbeat_removal_preserves_device_and_original_facts(tmp_path, monkeypatch):
    path = tmp_path / "read-only-device.db"
    _seed_0118(path, monkeypatch)
    config = Config("alembic.ini")
    command.upgrade(config, "0136")
    with sqlite3.connect(path) as connection:
        person = connection.execute("SELECT id FROM persons LIMIT 1").fetchone()[0]
        connection.execute(
            "INSERT INTO client_devices"
            "(id,person_id,token_hash,label,created_at,last_used_at,revoked_at) "
            "VALUES(?,?,?,'retained device',?,?,?)",
            (str(uuid4()), person, "d" * 64, "2026-10-18", "2026-10-18", "2026-10-18"),
        )
        retained = [
            row[1]
            for row in connection.execute("PRAGMA table_info(client_devices)")
            if row[1] != "last_used_at"
        ]
        columns = ",".join(retained)
        devices = connection.execute(f"SELECT {columns} FROM client_devices").fetchall()
        facts = {
            table: connection.execute(f'SELECT * FROM "{table}" ORDER BY rowid').fetchall()
            for table in ("persons", "identity_bindings", "chat_events", "canonical_event_receipts")
        }
    command.upgrade(config, "head")
    with sqlite3.connect(path) as connection:
        assert "last_used_at" not in {
            row[1] for row in connection.execute("PRAGMA table_info(client_devices)")
        }
        assert connection.execute(f"SELECT {columns} FROM client_devices").fetchall() == devices
        for table, rows in facts.items():
            assert connection.execute(f'SELECT * FROM "{table}" ORDER BY rowid').fetchall() == rows
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()
    command.downgrade(config, "0136")
    with sqlite3.connect(path) as connection:
        assert connection.execute(f"SELECT {columns} FROM client_devices").fetchall() == devices
        assert connection.execute("SELECT last_used_at FROM client_devices").fetchone() == (None,)
