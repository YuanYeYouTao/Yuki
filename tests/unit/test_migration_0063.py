"""The explicit-delivery migration removes retired state and policy data."""

import hashlib
import importlib
import json

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, text


def _hash(script: dict[str, object]) -> str:
    payload = json.loads(json.dumps(script))
    limits = payload.get("limits")
    if isinstance(limits, dict) and not limits.get("agent_budget_managed", False):
        limits.pop("agent_budget_managed", None)
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def test_remove_reply_effect_state_and_retired_overrides(monkeypatch) -> None:
    migration = importlib.import_module("migrations.versions.0063_remove_reply_effect_cadence")
    engine = create_engine("sqlite:///:memory:")
    script = {
        "version": 1,
        "name": "旧提醒",
        "timezone": "Asia/Shanghai",
        "schedule": {"type": "after", "seconds": 60},
        "steps": [
            {
                "id": "send",
                "call": "social.send_message",
                "arguments": {"text": "你好", "reply_state": {"effects": ["legacy"]}},
            }
        ],
        "limits": {
            "max_steps": 1,
            "max_llm_calls": 0,
            "max_tool_calls": 1,
            "max_messages": 1,
            "timeout_seconds": 30,
            "agent_budget_managed": False,
        },
    }
    with engine.begin() as connection:
        for table in ("automations", "automation_versions"):
            connection.execute(
                text(
                    f"CREATE TABLE {table} ("
                    "id INTEGER PRIMARY KEY, script_json TEXT NOT NULL, script_hash TEXT NOT NULL)"
                )
            )
            connection.execute(
                text(
                    f"INSERT INTO {table} (id, script_json, script_hash) VALUES (1, :script, 'old')"
                ),
                {"script": json.dumps(script, ensure_ascii=False)},
            )
        connection.execute(text("CREATE TABLE reply_effect_events (id INTEGER PRIMARY KEY)"))
        connection.execute(
            text(
                "CREATE TABLE runtime_config_overrides ("
                "config_key TEXT NOT NULL, scope_type TEXT NOT NULL DEFAULT 'global', "
                "canonical_person_id TEXT, canonical_space_id TEXT)"
            )
        )
        for key in (
            "speech.spontaneous_frequency",
            "emoji.spontaneous_frequency",
            "emoji.max_effects_per_reply",
            "reply.cancel_on_new_message",
            "speech.agent_effects_enabled",
            "reply.hard_max_messages",
        ):
            connection.execute(
                text("INSERT INTO runtime_config_overrides (config_key) VALUES (:key)"),
                {"key": key},
            )

        monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
        migration.upgrade()

        expected = json.loads(json.dumps(script))
        expected["steps"][0]["arguments"].pop("reply_state")
        for table in ("automations", "automation_versions"):
            row = connection.execute(
                text(f"SELECT script_json, script_hash FROM {table} WHERE id = 1")
            ).one()
            assert json.loads(row.script_json) == expected
            assert row.script_hash == _hash(expected)
        remaining = (
            connection.execute(text("SELECT config_key FROM runtime_config_overrides"))
            .scalars()
            .all()
        )
        dropped = connection.execute(
            text(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name = 'reply_effect_events'"
            )
        ).scalar_one_or_none()

    assert remaining == ["speech.agent_delivery_enabled", "reply.hard_max_messages"]
    assert dropped is None
