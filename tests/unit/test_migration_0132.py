"""ACP migration preserves every original receipt and execution field."""

import asyncio
import sqlite3
from uuid import uuid4

from alembic import command
from alembic.config import Config
from tests.unit.test_migration_0119 import _seed_0118

from yuki.client_api.identity import ClientIdentityService
from yuki.client_api.sessions import ClientSessions
from yuki.persistence.database import Database
from yuki.persistence.schema_guard import require_canonical_schema


def test_0132_preserves_original_rows_and_adds_real_acp_request_scope(tmp_path, monkeypatch):
    path = tmp_path / "acp-upgrade.sqlite3"
    url, _ = _seed_0118(path, monkeypatch)
    config = Config("alembic.ini")
    command.upgrade(config, "0131")
    with sqlite3.connect(path) as connection:
        person = connection.execute("SELECT id FROM persons LIMIT 1").fetchone()[0]
        connection.execute(
            "INSERT INTO admin_operation_events(actor_user_id,trigger_message_id,conversation_key,"
            "capability,operation,target_type,target_id,before_json,after_json,success,"
            "duration_seconds,created_at) VALUES(?, '', '', 'client.upload', 'client.upload',"
            "'person',?,'{}','{\"media_id\":\"original\"}',1,0,'2026-10-17')",
            (person, person),
        )
        audit = connection.execute("SELECT max(id) FROM admin_operation_events").fetchone()[0]
        for scope in (None, person):
            connection.execute(
                "INSERT INTO control_command_receipts(principal_id,request_id,request_person_id,"
                "payload_hash,status,audit_id,operation_kind,operation_ref,result_resource_id,"
                "effective_state_json,created_at,updated_at) VALUES(?,?,?,?,'succeeded',?,"
                "'client.upload',?,'original','{\"original\":\"paid result\"}',"
                "'2026-10-17','2026-10-17')",
                (str(uuid4()), str(uuid4()), scope, "a" * 64, audit, person),
            )
        receipt_columns = [
            row[1] for row in connection.execute("PRAGMA table_info(control_command_receipts)")
        ]
        receipts = connection.execute(
            "SELECT * FROM control_command_receipts ORDER BY id"
        ).fetchall()
        presences = connection.execute("SELECT * FROM presences ORDER BY id").fetchall()
        before = {
            table: connection.execute(f'SELECT * FROM "{table}"').fetchall()
            for (table,) in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            if table not in {"control_command_receipts", "presences", "alembic_version"}
        }
    command.upgrade(config, "0132")
    with sqlite3.connect(path) as connection:
        assert (
            connection.execute(
                "SELECT " + ",".join(receipt_columns) + " FROM control_command_receipts ORDER BY id"
            ).fetchall()
            == receipts
        )
        assert connection.execute(
            "SELECT request_conversation_id FROM control_command_receipts"
        ).fetchall() == [(None,), (None,)]
        assert (
            connection.execute(
                "SELECT * FROM presences WHERE platform <> 'acp' ORDER BY id"
            ).fetchall()
            == presences
        )
        assert connection.execute(
            "SELECT platform,external_account_id,ingest_eligible FROM presences "
            "WHERE platform='acp'"
        ).fetchall() == [("acp", "yuki-acp", 0)]
        for table, rows in before.items():
            assert connection.execute(f'SELECT * FROM "{table}"').fetchall() == rows
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()

    # The assertions above cover the frozen 0132 boundary. Exercise today's
    # identity services only after bringing that preserved database to head.
    command.upgrade(config, "head")

    async def exercise():
        await require_canonical_schema(url)
        database = Database(url)
        try:
            identity = ClientIdentityService(database)
            code = await identity.issue_for_operator(person, str(uuid4()), str(uuid4()))
            device = await identity.pair(code["code"], "migration client")
            base = await identity.authenticate(device["token"])
            entries = [await ClientSessions(identity).create(base, str(uuid4())) for _ in range(2)]
            request_id = str(uuid4())
            results = [await identity.issue_for_device(base, request_id)]
            for entry in entries:
                principal = await identity.authenticate(
                    device["token"], conversation_id=entry["session_id"]
                )
                results.append(await identity.issue_for_device(principal, request_id))
            assert len({item["code_id"] for item in results}) == 3
            assert entries[0]["binding_id"] == entries[1]["binding_id"]
        finally:
            await database.close()

    asyncio.run(exercise())
    with sqlite3.connect(path) as connection:
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()
