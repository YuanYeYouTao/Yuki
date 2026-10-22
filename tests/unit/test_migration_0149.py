"""Real 0147 upgrade preserves paid facts and separates chat from computer grants."""

import asyncio
import json
import re
import shutil
import sqlite3
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import event
from sqlalchemy.engine import Engine
from tests.support.client_identity import client_identity_service
from tests.unit.test_migration_0119 import _seed_0118

from yuki.client_api.errors import ClientError
from yuki.external_agents.identity import ConnectorIdentityService
from yuki.external_agents.repository import execution_key, instance_key, session_key
from yuki.identity.credentials import DeviceCredentials, secret_hash
from yuki.persistence.database import Database
from yuki.persistence.schema_guard import CanonicalSchemaError, require_canonical_schema


@pytest.fixture(scope="module")
def old_agent_database(tmp_path_factory):
    path = tmp_path_factory.mktemp("old-agent-db") / "0147.db"
    with pytest.MonkeyPatch.context() as environment:
        _seed_0118(path, environment)
        command.upgrade(Config("alembic.ini"), "0147")
    old_device, old_code, old_run = (str(uuid4()) for _ in range(3))
    token, code = "old-chat-token", "old-chat-code"
    with sqlite3.connect(path) as connection, connection:
        connection.execute("PRAGMA foreign_keys=ON")
        person = connection.execute("SELECT id FROM persons ORDER BY id LIMIT 1").fetchone()[0]
        conversation = connection.execute(
            "SELECT id FROM canonical_conversations ORDER BY id LIMIT 1"
        ).fetchone()[0]
        connection.execute(
            "INSERT INTO client_devices(id,person_id,token_hash,label,created_at) "
            "VALUES(?,?,?,'Existing chat device','2026-10-22')",
            (old_device, person, secret_hash(token)),
        )
        connection.execute(
            "INSERT INTO client_link_codes(id,code_hash,person_id,issuer_principal_id,"
            "issuer_source,created_at,expires_at) VALUES(?,?,?,?,'control',"
            "'2026-10-22','2099-10-22')",
            (old_code, secret_hash(code), person, str(uuid4())),
        )
        receipt = json.dumps(
            {
                "kind": "acp_prompt",
                "run_id": old_run,
                "peer_id": str(uuid4()),
                "status": "succeeded",
                "pending": False,
                "output": "Original paid result",
            }
        )
        connection.execute(
            "INSERT INTO sandbox_task_runs(request_id,source_conversation_id,source_json,"
            "payload_hash,run_id,status,completion_json,progress_json,created_at,updated_at) "
            "VALUES('paid-original',?,?,?,?,'completed',?,?,'2026-10-22','2026-10-22')",
            (
                conversation,
                '{"generation":1,"work_id":"original-work"}',
                "a" * 64,
                old_run,
                receipt,
                '{"kind":"acp_prompt","peer_id":"original-peer"}',
            ),
        )
        before = {
            table: connection.execute(f'SELECT * FROM "{table}"').fetchall()
            for table in (
                "chat_events",
                "sandbox_task_runs",
                "runtime_work_budgets",
                "runtime_work_journal",
                "runtime_work_effects",
            )
        }
    return path, person, old_device, old_code, token, code, before


@pytest.fixture
def upgraded_agents(old_agent_database, tmp_path, monkeypatch):
    source, *original = old_agent_database
    path = tmp_path / "upgrade.db"
    shutil.copyfile(source, path)
    url = f"sqlite+aiosqlite:///{path}"
    monkeypatch.setenv("DATABASE_URL", url)
    command.upgrade(Config("alembic.ini"), "0149")
    return path, url, *original


def test_0149_preserves_original_paid_rows_and_old_credentials_stay_chat(upgraded_agents):
    path, url, person, device, code_id, token, code, before = upgraded_agents
    with sqlite3.connect(path) as connection:
        for table, rows in before.items():
            assert connection.execute(f'SELECT * FROM "{table}"').fetchall() == rows
        assert connection.execute(
            "SELECT purpose FROM client_devices WHERE id=?",
            (device,),
        ).fetchone() == ("chat",)
        assert connection.execute(
            "SELECT purpose,used_at FROM client_link_codes WHERE id=?",
            (code_id,),
        ).fetchone() == ("chat", None)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []

    async def authenticate():
        database = Database(url)
        try:
            credentials = DeviceCredentials(database)
            connector = ConnectorIdentityService(database)
            async with database.read_session() as session:
                assert (
                    await credentials.authenticate(
                        session,
                        token,
                        purpose="chat",
                    )
                ).source_person_id == person
                assert (
                    await credentials.code(
                        session,
                        code,
                        datetime.now(UTC),
                        purpose="chat",
                    )
                ).id == code_id
            with pytest.raises(ClientError, match="unauthenticated"):
                await connector.authenticate(token)
            with pytest.raises(ClientError, match="unauthenticated"):
                await connector.pair(code, "Must not become a computer")
            paired = await client_identity_service(database).pair(code, "Original chat client")
            async with database.read_session() as session:
                assert (
                    await credentials.authenticate(
                        session,
                        paired["token"],
                        purpose="chat",
                    )
                ).source_person_id == person
        finally:
            await database.close()

    asyncio.run(authenticate())


def test_0149_native_namespaces_foreign_keys_and_event_lookup_are_real(upgraded_agents):
    path, _url, person, old_device, *_ = upgraded_agents
    with sqlite3.connect(path) as connection, connection:
        connection.execute("PRAGMA foreign_keys=ON")
        other_device = str(uuid4())
        connection.execute(
            "INSERT INTO client_devices(id,person_id,token_hash,label,created_at,purpose) "
            "VALUES(?,?,?,'Explicit computer','2026-10-23','connector')",
            (other_device, person, "c" * 64),
        )
        instances, sessions = [], []
        for device in (old_device, other_device):
            instance = instance_key(device, "same-profile")
            session = session_key(instance, "same-native-thread")
            execution = execution_key(session, "same-native-turn")
            connection.execute(
                "INSERT INTO external_agent_instances VALUES(?,?,?,'{}')",
                (instance, device, "same-profile"),
            )
            connection.execute(
                "INSERT INTO external_agent_sessions VALUES(?,?,?)",
                (session, instance, "same-native-thread"),
            )
            connection.execute(
                "INSERT INTO external_agent_executions VALUES(?,?,?)",
                (execution, session, "same-native-turn"),
            )
            instances.append(instance)
            sessions.append(session)
        assert len(set(instances)) == len(set(sessions)) == 2
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
            connection.execute(
                "INSERT INTO external_agent_instances VALUES(?,?,?,'{}')",
                (str(uuid4()), old_device, "same-profile"),
            )
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            connection.execute(
                "INSERT INTO external_agent_sessions VALUES(?,?,?)",
                (str(uuid4()), "missing-instance", "native"),
            )
        operation = str(uuid4())
        connection.execute(
            "INSERT INTO external_agent_operations(id,instance_id,session_id,actor_person_id,"
            "source_json,action,payload_hash,command_json,run_id,created_at) "
            "VALUES(?,?,?,?,'{}','prompt',?,'{}',?,'2026-10-23')",
            (operation, instances[1], sessions[1], person, "d" * 64, str(uuid4())),
        )
        for index in range(40):
            connection.execute(
                "INSERT INTO external_agent_events(device_id,event_id,operation_id,kind,"
                "payload_json,created_at) VALUES(?,?,?,'progress','{}','2026-10-23')",
                (other_device, f"event-{index}", operation),
            )
        query = "SELECT id FROM external_agent_events WHERE operation_id=? AND id>? ORDER BY id"
        page = connection.execute(query, (operation, 20)).fetchall()
        assert len(page) == 20 and page[0][0] == 21
        plan = connection.execute("EXPLAIN QUERY PLAN " + query, (operation, 20)).fetchall()
        assert any("SEARCH" in row[-1] and "operation_id" in row[-1] for row in plan)
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            connection.execute(
                "INSERT INTO external_agent_events(device_id,event_id,operation_id,kind,"
                "payload_json,created_at) "
                "VALUES(?,?,'missing-operation','progress','{}','2026-10-23')",
                (other_device, str(uuid4())),
            )
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_0149_startup_checks_metadata_without_scanning_original_or_agent_data(upgraded_agents):
    _path, url, *_ = upgraded_agents
    statements = []

    def observe(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement.lower())

    event.listen(Engine, "before_cursor_execute", observe)
    try:
        asyncio.run(require_canonical_schema(url))
    finally:
        event.remove(Engine, "before_cursor_execute", observe)
    assert statements
    assert not any(
        re.search(
            r'\bfrom\s+["`]?((external_agent_\w+)|(client_devices)|(client_link_codes)|'
            r"(sandbox_task_runs)|(chat_events))\b",
            statement,
        )
        for statement in statements
    )
    assert not any("foreign_key_check" in statement for statement in statements)


def test_0149_startup_still_requires_native_operation_storage(upgraded_agents):
    path, url, *_ = upgraded_agents
    with sqlite3.connect(path) as connection, connection:
        connection.execute("DROP TABLE external_agent_operations")
    with pytest.raises(CanonicalSchemaError, match="canonical schema is incomplete"):
        asyncio.run(require_canonical_schema(url))
