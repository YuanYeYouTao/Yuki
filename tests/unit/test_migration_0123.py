"""Existing receipts survive content-uniqueness retirement with operation identity intact."""

import sqlite3
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from tests.unit.test_migration_0119 import _seed_0118


def test_memory_operation_upgrade_preserves_original_receipt_and_independent_operations(
    tmp_path, monkeypatch
):
    path = tmp_path / "memory-operations.db"
    _seed_0118(path, monkeypatch)
    config = Config("alembic.ini")
    command.upgrade(config, "0122")
    values = {
        "id": 55,
        "mutation_id": str(uuid4()),
        "idempotency_key": "first-original-call",
        "claim_fingerprint": "same-content",
        "target_fingerprint": "same-owner",
        "trigger_source_type": "chat_event",
        "trigger_event_id": 2,
        "conversation_key": "original-conversation",
        "turn_origin": "user_message",
        "delegation_mode": "direct",
        "trigger_actor_user_id": "original-person",
        "decision_actor_type": "agent",
        "requested_operation": "create",
        "applied_operation": "noop",
        "outcome": "no_change",
        "reason_code": "original-result",
        "created_at": "2026-10-16 00:00:00",
    }
    columns = ",".join(values)
    placeholders = ",".join("?" for _ in values)
    insert = f"INSERT INTO memory_mutation_receipts ({columns}) VALUES ({placeholders})"
    with sqlite3.connect(path) as connection:
        connection.execute(insert, tuple(values.values()))
        original = connection.execute("SELECT * FROM memory_mutation_receipts").fetchall()
        events = connection.execute("SELECT * FROM chat_events ORDER BY id").fetchall()
    command.upgrade(config, "0123")
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT * FROM memory_mutation_receipts").fetchall() == original
        assert connection.execute("SELECT * FROM chat_events ORDER BY id").fetchall() == events
        second = dict(values, id=56, mutation_id=str(uuid4()), idempotency_key="second-call")
        connection.execute(insert, tuple(second.values()))
        with pytest.raises(sqlite3.IntegrityError, match="idempotency_key"):
            connection.execute(
                insert, tuple(dict(second, id=57, mutation_id=str(uuid4())).values())
            )
        assert connection.execute("SELECT COUNT(*) FROM memory_mutation_receipts").fetchone() == (
            2,
        )
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()
