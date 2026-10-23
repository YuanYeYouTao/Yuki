"""Unused telemetry retires without changing source, receipt or trace records."""

import asyncio
import gzip
import hashlib
import json
import sqlite3
from collections import Counter

import pytest
from alembic import command
from alembic.config import Config
from tests.unit.test_migration_0119 import _seed_0118

from yuki.persistence.schema_guard import require_canonical_schema


def test_trace_index_upgrade_preserves_payloads_and_actual_query_indexes(tmp_path, monkeypatch):
    path = tmp_path / "trace-indexes.db"
    url, _identities = _seed_0118(path, monkeypatch)
    config = Config("alembic.ini")
    command.upgrade(config, "0133")
    payload = json.dumps({"data": {"original": "实际请求原件"}, "redactions": []}).encode()
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO execution_trace_entries(turn_id,operation_id,kind,payload_status,"
            "payload_gzip,payload_sha256,payload_bytes,created_at,expires_at)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (
                "original-turn",
                "original-operation",
                "provider_start",
                "recorded",
                gzip.compress(payload, mtime=0),
                hashlib.sha256(payload).hexdigest(),
                len(payload),
                "2026-10-18 00:00:00",
                "2026-11-18 00:00:00",
            ),
        )
        connection.execute(
            "INSERT INTO tool_invocations(conversation_key_hash,provider_id,tool_name,success,"
            "latency_seconds,result_size,artifact_created,created_at) VALUES(?,?,?,?,?,?,?,?)",
            ("retired-hash", "original-provider", "lookup", 1, 0.1, 20, 0, "2026-10-18"),
        )
        connection.execute(
            "INSERT INTO runtime_turn_observations(runtime_turn_id,origin,scope_type,handled,"
            "sent_messages,total_latency_ms,created_at,expires_at) VALUES(?,?,?,?,?,?,?,?)",
            ("retired-turn", "message", "group", 1, 1, 200, "2026-10-18", "2026-11-18"),
        )
        retired_tables = {"tool_invocations", "runtime_turn_observations"}
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        # Compare all surviving facts, including paid model and actual tool receipts.
        surviving = tables - retired_tables - {"alembic_version", "sqlite_sequence"}
        facts = {
            table: Counter(connection.execute(f'SELECT * FROM "{table}"').fetchall())
            for table in surviving
        }
        before = connection.execute("SELECT * FROM execution_trace_entries ORDER BY id").fetchall()
        indexes = {
            row[1] for row in connection.execute("PRAGMA index_list(execution_trace_entries)")
        }
    command.upgrade(config, "0134")
    retired = {"ix_execution_trace_roots", "ix_execution_trace_operation_id"}
    with sqlite3.connect(path) as connection:
        assert {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        } == tables - retired_tables
        for table, rows in facts.items():
            assert Counter(connection.execute(f'SELECT * FROM "{table}"').fetchall()) == rows
        assert (
            connection.execute("SELECT * FROM execution_trace_entries ORDER BY id").fetchall()
            == before
        )
        assert {
            row[1] for row in connection.execute("PRAGMA index_list(execution_trace_entries)")
        } == indexes - retired
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()
    with pytest.raises(RuntimeError, match="Retired diagnostic rows cannot be reconstructed"):
        command.downgrade(config, "0133")
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == ("0134",)
        assert {
            row[1] for row in connection.execute("PRAGMA index_list(execution_trace_entries)")
        } == indexes - retired
        assert (
            connection.execute("SELECT * FROM execution_trace_entries ORDER BY id").fetchall()
            == before
        )
    command.upgrade(config, "head")
    asyncio.run(require_canonical_schema(url))
