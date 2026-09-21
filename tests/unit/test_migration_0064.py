"""The Agent tool-contract migration removes inert per-task allowlists."""

import importlib
import json

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, text


def test_remove_agent_tool_allowlist_and_refresh_hash(monkeypatch) -> None:
    migration = importlib.import_module("migrations.versions.0064_remove_agent_tool_allowlist")
    engine = create_engine("sqlite:///:memory:")
    legacy = {
        "steps": [
            {
                "id": "agent",
                "call": "yuki.agent",
                "arguments": {
                    "instruction": "检查任务",
                    "allowed_capabilities": [
                        "social.send_private_message",
                        "social.send_group_message",
                    ],
                },
            },
            {
                "id": "delivery",
                "call": "onebot.send_group_message",
                "arguments": {"group_id": "123", "text": "done"},
            },
        ],
        "limits": {"agent_budget_managed": False},
    }
    untouched = {
        "steps": [
            {
                "id": "plugin",
                "call": "plugin.run",
                "arguments": {"allowed_capabilities": ["plugin.read"]},
            }
        ]
    }
    with engine.begin() as connection:
        for table in ("automations", "automation_versions"):
            connection.execute(
                text(
                    f"CREATE TABLE {table} ("
                    "id TEXT PRIMARY KEY, script_json TEXT NOT NULL, script_hash TEXT NOT NULL)"
                )
            )
            connection.execute(
                text(
                    f"INSERT INTO {table} (id, script_json, script_hash) "
                    "VALUES ('legacy', :legacy, 'old'), ('untouched', :untouched, 'keep')"
                ),
                {
                    "legacy": json.dumps(legacy),
                    "untouched": json.dumps(untouched),
                },
            )
        monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
        migration.upgrade()
        for table in ("automations", "automation_versions"):
            rows = connection.execute(
                text(f"SELECT id, script_json, script_hash FROM {table} ORDER BY id")
            ).fetchall()
            migrated = json.loads(rows[0][1])
            assert "allowed_capabilities" not in migrated["steps"][0]["arguments"]
            assert migrated["steps"][1]["call"] == "onebot.send_group_message"
            assert rows[0][2] != "old"
            assert json.loads(rows[1][1]) == untouched
            assert rows[1][2] == "keep"
