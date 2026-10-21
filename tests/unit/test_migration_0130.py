"""Retiring Host capacities changes current overrides, never historical execution facts."""

import asyncio
import json
import sqlite3
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from tests.conftest import make_settings
from tests.unit.test_migration_0119 import _seed_0118

from yuki.admin.config_service import RuntimeConfigService
from yuki.persistence.database import Database


@pytest.mark.parametrize("existing_trigger", [None, 70000])
def test_0130_deletes_only_current_capacity_overrides_in_every_scope(
    tmp_path, monkeypatch, existing_trigger
):
    path = tmp_path / "trigger-upgrade.db"
    url, _ = _seed_0118(path, monkeypatch)
    config = Config("alembic.ini")
    command.upgrade(config, "0129")
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        person = connection.execute("SELECT id FROM persons LIMIT 1").fetchone()[0]
        space = str(uuid4())
        connection.execute(
            "INSERT INTO spaces(id,name,enabled,autonomous_enabled,require_mention,revision,"
            "created_at,updated_at) VALUES(?,'original space',1,1,1,1,'2026-10-17','2026-10-17')",
            (space,),
        )
        overrides = [
            (key, scope, person if scope == "user" else None, space if scope == "group" else None)
            for key in ("context.window_tokens", "context.work_window_tokens")
            for scope in ("global", "user", "group")
        ]
        overrides.append(("context.window_tokens_extra", "global", None, None))
        for key, scope, person_id, space_id in overrides:
            connection.execute(
                "INSERT INTO runtime_config_overrides(config_key,scope_type,canonical_person_id,"
                "canonical_space_id,value_json,value_type,apply_mode,version,created_at,"
                "updated_at,updated_by) VALUES(?,?,?,?,'524288','integer','hot',3,"
                "'2026-10-02','2026-10-02','original operator')",
                (key, scope, person_id, space_id),
            )
        if existing_trigger is not None:
            connection.execute(
                "INSERT INTO runtime_config_overrides(config_key,scope_type,value_json,"
                "value_type,apply_mode,version,created_at,updated_at,updated_by) VALUES("
                "'context.compact_trigger_tokens','global',?,'integer','hot',1,"
                "'2026-10-17','2026-10-17','new policy operator')",
                (json.dumps(existing_trigger),),
            )
        connection.execute(
            "INSERT INTO admin_operation_events(actor_user_id,trigger_message_id,conversation_key,"
            "capability,operation,target_type,target_id,before_json,after_json,success,"
            "duration_seconds,created_at) VALUES('original operator','','','runtime_config',"
            "'set_override','config.global','context.window_tokens','null',"
            "'{\"value\":524288}',1,0,'2026-10-02')"
        )
        tables = [
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            if row[0] not in {"runtime_config_overrides", "alembic_version"}
        ]
        before = {
            table: connection.execute(f'SELECT * FROM "{table}"').fetchall() for table in tables
        }
        remaining = connection.execute(
            "SELECT * FROM runtime_config_overrides WHERE config_key NOT IN (?,?) ORDER BY id",
            ("context.window_tokens", "context.work_window_tokens"),
        ).fetchall()

    command.upgrade(config, "0130")
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == ("0130",)
        assert (
            connection.execute("SELECT * FROM runtime_config_overrides ORDER BY id").fetchall()
            == remaining
        )
        for table, rows in before.items():
            assert connection.execute(f'SELECT * FROM "{table}"').fetchall() == rows
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()

    async def snapshot():
        database = Database(url)
        try:
            runtime = RuntimeConfigService(settings=make_settings(url), database=database)
            return await runtime.snapshot()
        finally:
            await database.close()

    assert asyncio.run(snapshot()).context.compact_trigger_tokens == (
        96000 if existing_trigger is None else existing_trigger
    )
