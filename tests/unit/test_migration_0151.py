"""Frozen definition upgrade preserves original identities, bytes and occurrence facts."""

import asyncio
import importlib
import json
import sqlite3

import pytest
from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError

from yuki.persistence.schema_guard import require_canonical_schema


@pytest.mark.parametrize("interrupt_switch", [False, True])
def test_single_upgrade_preserves_originals_and_publishes_only_future_definitions(
    monkeypatch, interrupt_switch
):
    migration = importlib.import_module("migrations.versions.0151_automation_definitions")
    with create_engine("sqlite://").connect() as connection:
        for ddl in (
            "CREATE TABLE persons(id TEXT PRIMARY KEY)",
            "CREATE TABLE account_assignments(id TEXT PRIMARY KEY,kind TEXT)",
            "CREATE TABLE canonical_conversations(id TEXT PRIMARY KEY)",
            "CREATE TABLE chat_events(id INTEGER PRIMARY KEY)",
            "CREATE TABLE spaces(id TEXT PRIMARY KEY)",
            "CREATE TABLE automation_runs(id INTEGER PRIMARY KEY,version_id INTEGER,receipt TEXT)",
            "CREATE TABLE runtime_work(id TEXT PRIMARY KEY,source_json TEXT,checkpoint_json TEXT)",
            "CREATE TABLE automation_versions(id INTEGER PRIMARY KEY,automation_id INTEGER,"
            "version INTEGER,script_json TEXT,script_hash TEXT,authority_snapshot_json TEXT,"
            "canonical_target_person_id TEXT,canonical_target_space_id TEXT,"
            "canonical_presence_id TEXT,updated_by TEXT,created_at TEXT)",
            "CREATE TABLE automations(id INTEGER PRIMARY KEY,creator_kind TEXT,"
            "creator_user_id TEXT,bot_user_id TEXT,name TEXT,status TEXT,"
            "timezone TEXT,schedule_json TEXT,script_json TEXT,"
            "script_hash TEXT,required_capabilities_json TEXT,authority_snapshot_json TEXT,"
            "created_from_message_id TEXT,creation_source_key TEXT,"
            "next_run_at TEXT,last_run_at TEXT,"
            "run_count INTEGER,max_runs INTEGER,consecutive_failures INTEGER,claimed_by TEXT,"
            "claimed_until TEXT,created_at TEXT,updated_at TEXT,canonical_creator_person_id TEXT,"
            "canonical_target_person_id TEXT,canonical_target_space_id TEXT,"
            "canonical_presence_id TEXT)",
        ):
            connection.execute(text(ddl))
        connection.execute(text("INSERT INTO persons VALUES('person')"))
        connection.execute(text("INSERT INTO account_assignments VALUES('presence','yuki')"))
        connection.execute(text("INSERT INTO canonical_conversations VALUES('conversation')"))
        connection.execute(text("INSERT INTO spaces VALUES('space')"))
        scene = json.dumps(
            {
                "creator_user_id": "original-account",
                "bot_user_id": "original-bot",
                "created_from_message_id": "native-message",
                "created_at": "original-time",
                "authority_version": 1,
                "origin": "scheduled_automation",
                "permission_level": "user",
                "canonical_creator_person_id": "person",
                "canonical_conversation_id": "conversation",
                "canonical_presence_id": "presence",
                "canonical_space_id": "space",
                "current_group_id": "original-group",
                "conversation_generation": 4,
                "principal_kind": "person",
            }
        )
        scripts = (
            {
                "goal": "到时发送提醒" + "完整原文" * 2000,
                "trigger": {"type": "after", "seconds": 3600},
                "constraints": ["保留顺序一", '{"原始":"JSON约束尾部"}'],
                "delivery": {"target": "none"},
            },
            {
                "goal": "每天执行",
                "trigger": {
                    "type": "daily",
                    "hour": 9,
                    "minute": 3,
                    "timezone": "America/New_York",
                },
                "delivery": {"target": "current_group"},
            },
            {
                "goal": "单次执行",
                "trigger": {"type": "once", "local_datetime": "2026-10-01T09:00:00"},
                "delivery": {"target": "self_private"},
            },
            {
                "name": "旧程序",
                "schedule": {"type": "interval", "seconds": 10},
                "steps": [{"call": "old.plugin"}],
            },
            {"goal": "已取消", "trigger": {"type": "interval", "seconds": 30}},
        )
        for index, original in enumerate(scripts, 1):
            raw = json.dumps(
                {
                    "version": 1,
                    "name": f"original-{index}",
                    "timezone": "Asia/Shanghai",
                    **original,
                },
                ensure_ascii=False,
            )
            values = {
                "id": index,
                "creator_kind": "person",
                "creator_user_id": "original-account",
                "bot_user_id": "original-bot",
                "name": f"original-{index}",
                "status": ("active", "paused", "failed", "blocked", "cancelled")[index - 1],
                "timezone": "Asia/Shanghai",
                "schedule_json": "{}",
                "script_json": raw,
                "script_hash": f"accepted-original-hash-{index}",
                "required_capabilities_json": "[]",
                "authority_snapshot_json": scene,
                "created_from_message_id": "native-message",
                "creation_source_key": f"call:{index}",
                "next_run_at": "2026-10-01 01:00:00",
                "last_run_at": "2026-09-30 01:00:00",
                "run_count": 7,
                "max_runs": 13,
                "consecutive_failures": 2,
                "claimed_by": "original-worker",
                "claimed_until": "2026-10-01 02:00:00",
                "created_at": "2026-09-01",
                "updated_at": "2026-09-20",
                "canonical_creator_person_id": "person",
                "canonical_target_person_id": "person" if index == 3 else None,
                "canonical_target_space_id": None if index == 3 else "space",
                "canonical_presence_id": "presence",
            }
            connection.execute(
                text(
                    "INSERT INTO automations ("
                    + ",".join(values)
                    + ") VALUES ("
                    + ",".join(":" + key for key in values)
                    + ")"
                ),
                values,
            )
            version = {
                key: values[key]
                for key in (
                    "script_json",
                    "script_hash",
                    "authority_snapshot_json",
                    "canonical_target_person_id",
                    "canonical_target_space_id",
                    "canonical_presence_id",
                    "created_at",
                )
            }
            if index == 1:
                version["authority_snapshot_json"] = None
            connection.execute(
                text(
                    "INSERT INTO automation_versions (id,automation_id,version,script_json,"
                    "script_hash,authority_snapshot_json,canonical_target_person_id,"
                    "canonical_target_space_id,canonical_presence_id,updated_by,created_at) "
                    "VALUES (:id,:id,1,:script_json,:script_hash,:authority_snapshot_json,"
                    ":canonical_target_person_id,:canonical_target_space_id,:canonical_presence_id,"
                    "'original-author',:created_at)"
                ),
                {"id": index, **version},
            )
        connection.execute(text("INSERT INTO automation_runs VALUES(1,1,'original-unknown-send')"))
        # A retained current owner may equal an earlier version rather than
        # the latest. Dropping the owner must preserve its actual current view.
        connection.execute(
            text(
                "INSERT INTO automation_versions SELECT 6,automation_id,2,:script,"
                "'another-old-hash',authority_snapshot_json,canonical_target_person_id,"
                "canonical_target_space_id,canonical_presence_id,updated_by,created_at "
                "FROM automation_versions WHERE id=4"
            ),
            {"script": json.dumps({"retained": True})},
        )
        connection.execute(
            text(
                "INSERT INTO runtime_work VALUES('original-work','original-source-and-hash',"
                "'original-paid-request')"
            )
        )

        def rows(table):
            return [
                dict(row)
                for row in connection.execute(text(f"SELECT * FROM {table} ORDER BY id")).mappings()
            ]

        originals = {
            table: rows(table)
            for table in ("automation_versions", "automation_runs", "runtime_work")
        }
        original_owners = rows("automations")
        connection.commit()
        if interrupt_switch:

            class InterruptedSwitch(Operations):
                def execute(self, statement, **kwargs):
                    if statement == "DROP TABLE automations":
                        connection.exec_driver_sql("INSERT INTO missing_migration_table VALUES (1)")
                    return super().execute(statement, **kwargs)

            monkeypatch.setattr(
                migration, "op", InterruptedSwitch(MigrationContext.configure(connection))
            )
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            with pytest.raises(OperationalError, match="missing_migration_table"):
                migration.upgrade()
            connection.rollback()
            assert rows("automations") == original_owners
            for table, original in originals.items():
                assert rows(table) == original
            assert "script_json" in {
                item[1] for item in connection.exec_driver_sql("PRAGMA table_info(automations)")
            }
            assert (
                connection.scalar(
                    text("SELECT name FROM sqlite_master WHERE name='automations_new'")
                )
                is None
            )
            connection.rollback()
        monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        migration.upgrade()
        connection.commit()
        assert rows("automation_versions")[:6] == originals["automation_versions"]
        assert len(rows("automation_versions")) == 11
        assert rows("automation_runs") == originals["automation_runs"]
        assert rows("runtime_work") == originals["runtime_work"]
        owners = rows("automations")
        assert all(row["canonical_conversation_id"] == "conversation" for row in owners)
        assert all(row["max_runs"] == row["creation_max_runs"] == 13 for row in owners)
        assert all(
            row["next_run_at"] == "2026-10-01 01:00:00" and row["run_count"] == 7 for row in owners
        )
        assert all(row["source_event_id"] is None for row in owners)
        assert (
            not {
                "script_json",
                "script_hash",
                "authority_snapshot_json",
                "required_capabilities_json",
                "consecutive_failures",
                "canonical_target_person_id",
            }
            & owners[0].keys()
        )
        latest = {
            index: connection.execute(
                text(
                    "SELECT * FROM automation_versions WHERE automation_id=:id "
                    "ORDER BY version DESC LIMIT 1"
                ),
                {"id": index},
            )
            .mappings()
            .one()
            for index in range(1, 6)
        }
        first = json.loads(latest[1]["script_json"])
        assert set(first) == {"goal", "trigger", "name"}
        assert first["goal"].startswith(scripts[0]["goal"])
        assert first["goal"].endswith('保留顺序一\n{"原始":"JSON约束尾部"}')
        assert "禁止发送" not in first["goal"]
        assert json.loads(latest[2]["script_json"])["trigger"]["timezone"] == "America/New_York"
        assert (
            json.loads(latest[3]["script_json"])["trigger"]["local_datetime"]
            == "2026-10-01T09:00:00+08:00"
        )
        for key in ("script_json", "script_hash", "authority_snapshot_json"):
            assert latest[4][key] == originals["automation_versions"][3][key]
        assert latest[4]["version"] > 2
        assert latest[5] == originals["automation_versions"][4]
        assert not connection.execute(text("PRAGMA foreign_key_check")).fetchall()


def test_real_0150_upgrade_preserves_role_trigger_and_passes_startup_guard(tmp_path, monkeypatch):
    path = tmp_path / "real-0150.db"
    url = f"sqlite+aiosqlite:///{path.as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    config = Config("alembic.ini")
    command.upgrade(config, "0150")

    def role_triggers():
        with sqlite3.connect(path) as connection:
            return {
                name: " ".join(sql.split())
                for name, sql in connection.execute(
                    "SELECT name,sql FROM sqlite_master WHERE type='trigger' "
                    "AND name LIKE 'trg_automations_canonical_presence_id_kind_%'"
                )
            }

    original = role_triggers()
    assert len(original) == 2
    command.upgrade(config, "head")
    assert role_triggers() == original
    asyncio.run(require_canonical_schema(url))
