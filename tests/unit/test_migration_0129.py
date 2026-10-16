"""Forward upgrade preserves history and receipts and backfills accountless names."""

import sqlite3
from uuid import uuid4

from alembic import command
from alembic.config import Config
from tests.unit.test_migration_0119 import _seed_0118


def test_0129_preserves_original_facts_and_migrates_names_and_receipt_fk(tmp_path, monkeypatch):
    path = tmp_path / "person-upgrade.db"
    _seed_0118(path, monkeypatch)
    config = Config("alembic.ini")
    command.upgrade(config, "0128")
    tables = (
        "chat_events",
        "runtime_work",
        "canonical_conversations",
        "conversation_legacy_aliases",
        "identity_bindings",
        "social_operation_receipts",
    )
    with sqlite3.connect(path) as connection:
        person = connection.execute("SELECT id FROM persons ORDER BY id LIMIT 1").fetchone()[0]
        connection.execute(
            "INSERT INTO person_aliases(canonical_person_id,alias,alias_type,"
            "first_seen_at,last_seen_at) VALUES (?,'Main name','nickname',"
            "'2026-10-16','2026-10-17')",
            (person,),
        )
        connection.execute(
            "INSERT INTO person_aliases(canonical_person_id,alias,alias_type,"
            "first_seen_at,last_seen_at) VALUES (?,'Old name','nickname',"
            "'2026-10-15','2026-10-16')",
            (person,),
        )
        connection.execute(
            "INSERT INTO control_command_receipts(principal_id,request_id,payload_hash,status,"
            "problem_code,created_at,updated_at) VALUES(?,?,?,'failed','not_found',?,?)",
            (str(uuid4()), str(uuid4()), "a" * 64, "2026-10-17", "2026-10-17"),
        )
        receipt_columns = [
            row[1] for row in connection.execute("PRAGMA table_info(control_command_receipts)")
        ]
        original_receipt = connection.execute("SELECT * FROM control_command_receipts").fetchall()
        before = {
            table: connection.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()
            for table in tables
        }
    command.upgrade(config, "0129")
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT display_name,merged_into_person_id FROM persons WHERE id=?", (person,)
        ).fetchone() == ("Main name", None)
        for table, rows in before.items():
            assert connection.execute(f"SELECT * FROM {table} ORDER BY id").fetchall() == rows
        assert (
            connection.execute(
                "SELECT " + ",".join(receipt_columns) + " FROM control_command_receipts"
            ).fetchall()
            == original_receipt
        )
        assert connection.execute(
            "SELECT request_person_id FROM control_command_receipts"
        ).fetchall() == [(None,)]
        assert not any(
            row[3] == "source_conversation_id"
            for row in connection.execute("PRAGMA foreign_key_list(social_operation_receipts)")
        )
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()
