"""0159 on an actual 0158 schema: executor facts and session-addressable events (G13).

Failure cases written first: the upgrade loses or mis-attributes an original
event, leaves session columns empty for stored facts, or lacks the session index.
"""

import asyncio
import json
import sqlite3

from alembic import command
from alembic.config import Config


def _rows(path, sql, *args):
    with sqlite3.connect(path) as connection:
        return connection.execute(sql, args).fetchall()


async def test_0159_backfills_event_instance_and_session_without_touching_originals(
    tmp_path, monkeypatch
):
    old = tmp_path / "actual-0158.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{old.as_posix()}")
    await asyncio.to_thread(command.upgrade, Config("alembic.ini"), "0158")
    events = [
        ("device-a", "e1", {"instance_id": "claude-local", "session_id": "native-s1"}),
        ("device-a", "e2", {"instance_id": "claude-local"}),
        ("device-a", "e3", {"instance_id": "unknown-local", "session_id": "native-s2"}),
        ("device-b", "e4", {"instance_id": "claude-local", "session_id": "native-s1"}),
    ]
    with sqlite3.connect(old) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        for device, local, identity in (
            ("device-a", "claude-local", "instance-a"),
            ("device-b", "claude-local", "instance-b"),
        ):
            connection.execute(
                "INSERT INTO external_agent_instances (id, device_id, local_id, catalog_json)"
                " VALUES (?, ?, ?, '{}')",
                (identity, device, local),
            )
        for device, event_id, payload in events:
            connection.execute(
                "INSERT INTO external_agent_events (device_id, event_id, operation_id, kind,"
                " payload_json, created_at) VALUES (?, ?, NULL, 'permission', ?, '2026-11-18')",
                (device, event_id, json.dumps(payload)),
            )
    before = _rows(old, "SELECT id, device_id, event_id, payload_json FROM external_agent_events")

    await asyncio.to_thread(command.upgrade, Config("alembic.ini"), "0159")

    assert (
        _rows(old, "SELECT id, device_id, event_id, payload_json FROM external_agent_events")
        == before
    )
    assert _rows(
        old,
        "SELECT event_id, instance_id, native_session_id FROM external_agent_events"
        " ORDER BY event_id",
    ) == [
        ("e1", "instance-a", "native-s1"),
        ("e2", "instance-a", None),
        ("e3", None, "native-s2"),
        ("e4", "instance-b", "native-s1"),
    ]
    index = _rows(old, "PRAGMA index_info('ix_external_agent_events_session')")
    assert [row[2] for row in index] == ["instance_id", "native_session_id", "kind", "id"]
    columns = {row[1] for row in _rows(old, "PRAGMA table_info('runtime_subagents')")}
    assert "executor_json" in columns

    await asyncio.to_thread(command.downgrade, Config("alembic.ini"), "0158")
    assert "instance_id" not in {
        row[1] for row in _rows(old, "PRAGMA table_info('external_agent_events')")
    }
