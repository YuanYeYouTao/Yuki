"""An upgrade never widens existing supervision or changes original receipts."""

import json
import sqlite3
from uuid import uuid4

from alembic import command
from alembic.config import Config
from tests.unit.test_migration_0149 import old_agent_database as old_agent_database
from tests.unit.test_migration_0149 import upgraded_agents as upgraded_agents


def test_existing_watch_scope_is_frozen_without_changing_receipts(upgraded_agents):
    path, _url, person, device, *_rest = upgraded_agents
    instance = str(uuid4())
    original = '{"status":"unknown","pending":true,"admitted":true}'
    commands = {
        "old-turn": {"action": "watch", "execution_id": "original-turn"},
        "old-idle": {"action": "watch"},
        "new-session": {"action": "watch", "observation_scope": "session"},
        "prompt": {"action": "prompt", "prompt": "original input"},
    }
    with sqlite3.connect(path) as connection, connection:
        connection.execute(
            "INSERT INTO external_agent_instances(id,device_id,local_id,catalog_json) "
            "VALUES(?,?,'profile','{}')",
            (instance, device),
        )
        for identity, value in commands.items():
            connection.execute(
                "INSERT INTO external_agent_operations("
                "id,instance_id,actor_person_id,source_json,action,payload_hash,command_json,"
                "result_json,created_at) VALUES(?,?,?,'{}',?,'original',?,?,'2026-10-23')",
                (identity, instance, person, value["action"], json.dumps(value), original),
            )
    command.upgrade(Config("alembic.ini"), "0150")
    with sqlite3.connect(path) as connection:
        for identity, raw, receipt in connection.execute(
            "SELECT id,command_json,result_json FROM external_agent_operations"
        ):
            expected = commands[identity]
            if identity.startswith("old-"):
                expected = {**expected, "observation_scope": "execution"}
            assert json.loads(raw) == expected
            assert receipt == original
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
