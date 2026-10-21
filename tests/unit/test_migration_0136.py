"""Person qualification starts unassigned; original receipts and requests survive."""

import sqlite3
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from tests.unit.test_migration_0119 import _seed_0118


def test_person_authority_migration_never_imports_accounts_or_rewrites_receipts(
    tmp_path, monkeypatch
):
    path = tmp_path / "person-authority.db"
    _seed_0118(path, monkeypatch)
    config = Config("alembic.ini")
    command.upgrade(config, "0135")
    with sqlite3.connect(path) as connection:
        people = connection.execute("SELECT * FROM persons ORDER BY id").fetchall()
        before = {
            table: connection.execute(f'SELECT * FROM "{table}" ORDER BY rowid').fetchall()
            for table in ("chat_events", "canonical_event_receipts", "identity_bindings")
        }
        original_columns = [item[1] for item in connection.execute("PRAGMA table_info(persons)")]
    monkeypatch.setenv("SUPERUSERS", "0,1,2")
    command.upgrade(config, "0136")
    with sqlite3.connect(path) as connection:
        columns = ",".join(original_columns)
        assert connection.execute(f"SELECT {columns} FROM persons ORDER BY id").fetchall() == people
        assert {row[0] for row in connection.execute("SELECT is_superuser FROM persons")} == {0}
        for table, rows in before.items():
            assert connection.execute(f'SELECT * FROM "{table}" ORDER BY rowid').fetchall() == rows
        now = "2026-10-18 00:00:00"

        def receipt(source, revision):
            connection.execute(
                "INSERT INTO control_command_receipts "
                "(principal_id,request_id,payload_hash,status,problem_code,created_at,updated_at,"
                "source_event_id,expected_revision) VALUES(?,?,?,'failed','not_found',?,?,?,?)",
                (str(uuid4()), str(uuid4()), "0" * 64, now, now, source, revision),
            )

        receipt(1234, 7)
        receipt(None, None)
        receipt(None, None)
        with pytest.raises(sqlite3.IntegrityError):
            receipt(1234, 8)
        assert connection.execute(
            "SELECT expected_revision FROM control_command_receipts WHERE source_event_id=1234"
        ).fetchone() == (7,)
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()
    command.downgrade(config, "0135")
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT * FROM persons ORDER BY id").fetchall() == people
        assert connection.execute("SELECT count(*) FROM control_command_receipts").fetchone() == (
            3,
        )
