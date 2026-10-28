"""The destructive pool retirement preserves identity, history and paid execution facts."""

import sqlite3

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from tests.support.migration_columns import preserved_projection
from tests.unit.test_migration_0119 import _seed_0118


def test_sticker_upgrade_retires_pool_and_active_config_but_keeps_original_facts(
    tmp_path, monkeypatch
):
    path = tmp_path / "sticker-upgrade.db"
    _seed_0118(path, monkeypatch)
    config = Config("alembic.ini")
    command.upgrade(config, "0137")
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO runtime_config_overrides(config_key,scope_type,value_json,value_type,"
            "apply_mode,version,created_at,updated_at,updated_by) "
            "VALUES('emoji.enabled','global','true','boolean','restart_required',"
            "1,'2026-10-19','2026-10-19','operator')"
        )
        connection.execute(
            "INSERT INTO runtime_config_overrides(config_key,scope_type,value_json,value_type,"
            "apply_mode,version,created_at,updated_at,updated_by) "
            "VALUES('vision.thinking_enabled','global','false','boolean','hot',"
            "1,'2026-10-19','2026-10-19','operator')"
        )
        facts = {
            table: connection.execute(f'SELECT * FROM "{table}" ORDER BY rowid').fetchall()
            for table in (
                "persons",
                "identity_bindings",
                "chat_events",
                "canonical_event_receipts",
                "control_command_receipts",
                "runtime_work",
            )
        }
        original_columns = {
            table: [row[1] for row in connection.execute(f"PRAGMA table_info({table})")]
            for table in facts
        }
    command.upgrade(config, "0138")
    with sqlite3.connect(path) as connection:
        names = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert not names.intersection(
            {
                "emoji_assets",
                "emoji_scope_states",
                "emoji_jobs",
                "emoji_usage_events",
                "emoji_descriptions",
            }
        )
        assert not names.intersection({"sticker_assets", "sticker_sources", "sticker_terms"})
        assert connection.execute(
            "SELECT config_key FROM runtime_config_overrides ORDER BY config_key"
        ).fetchall() == [("vision.thinking_enabled",)]
        for table, original in facts.items():
            assert (
                connection.execute(f'SELECT * FROM "{table}" ORDER BY rowid').fetchall() == original
            )
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()
    command.upgrade(config, "head")
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
            ScriptDirectory.from_config(config).get_current_head(),
        )
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE name='social_emoji_intents'"
        ).fetchone()
        for table, original in facts.items():
            if table == "identity_bindings":
                columns = ",".join(
                    "assignment.started_at"
                    if name == "history_starts_at"
                    else ("account." + name)
                    if name in {"platform", "external_account_id", "display_name"}
                    else ("assignment." + name)
                    for name in original_columns[table]
                )
                preserved = connection.execute(
                    f"SELECT {columns} FROM account_assignments assignment "
                    "JOIN accounts account ON account.id=assignment.account_id "
                    "WHERE assignment.kind='person'"
                ).fetchall()
                assert set(preserved) == set(original)
                continue
            columns = preserved_projection(connection, table, original_columns[table])
            assert (
                connection.execute(f'SELECT {columns} FROM "{table}" ORDER BY rowid').fetchall()
                == original
            )
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()
