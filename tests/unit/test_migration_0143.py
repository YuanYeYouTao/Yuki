"""The backend audit upgrade preserves facts and projects exact artifact references."""

import asyncio
import json
import shutil
import sqlite3
from contextlib import closing

import pytest
from alembic import command
from alembic.config import Config
from tests.unit.test_migration_0119 import _seed_0118

from yuki.persistence.artifact_refs import ARTIFACT_REFERENCE_TRIGGERS
from yuki.persistence.schema_guard import require_canonical_schema

PROTECTED = (
    "persons",
    "accounts",
    "account_assignments",
    "canonical_conversations",
    "chat_events",
    "canonical_event_receipts",
    "social_operation_receipts",
    "runtime_work",
    "runtime_work_effects",
    "runtime_work_journal",
    "runtime_protocol_refs",
    "prompt_projection_chunks",
    "plugin_agent_messages",
)


@pytest.fixture(scope="module")
def original_template(tmp_path_factory):
    path = tmp_path_factory.mktemp("backend-upgrade") / "0142.db"
    with pytest.MonkeyPatch.context() as patch:
        _seed_0118(path, patch)
        command.upgrade(Config("alembic.ini"), "0142")
    return path


def test_upgrade_preserves_sources_and_backfills_exact_pairs(
    original_template, tmp_path, monkeypatch
):
    path = tmp_path / "upgrade.db"
    shutil.copyfile(original_template, path)
    url = f"sqlite+aiosqlite:///{path}"
    monkeypatch.setenv("DATABASE_URL", url)
    with closing(sqlite3.connect(path)) as connection, connection:
        person = connection.execute("SELECT id FROM persons LIMIT 1").fetchone()[0]
        connection.execute(
            "INSERT INTO plugin_installations(plugin_id,name,version,plugin_api,yuki_requires,"
            "manifest_hash,entrypoint,status,enabled,approved_permissions_json,"
            "requested_permissions_json,failure_count,discovered_at,updated_at) "
            "VALUES('original-plugin','Original','1','6.0','>=3',?,'plugin:Plugin','approved',"
            "1,'[]','[]',0,'2026-10-20','2026-10-20')",
            ("a" * 64,),
        )
        connection.execute(
            "INSERT INTO plugin_agent_sessions(session_id,plugin_id,scope_type,name,model,"
            "instructions,persistence,context_profile,allowed_capabilities_json,status,"
            "next_sequence,turn_count,created_at,updated_at,last_active_at,"
            "canonical_owner_person_id) "
            "VALUES('original-session','original-plugin','user','Original','original-model',"
            "'Continue original story','durable','none','[\"unused\"]','active',2,1,"
            "'2026-10-20','2026-10-20','2026-10-20',?)",
            (person,),
        )
        connection.execute(
            "INSERT INTO plugin_agent_messages(session_id,sequence,role,content,metadata_json,"
            "created_at) VALUES('original-session',1,'assistant','original paid result',"
            "'{\"chapter\":1}','2026-10-20')"
        )
        event_id = connection.execute("SELECT id FROM chat_events LIMIT 1").fetchone()[0]
        connection.execute(
            "UPDATE chat_events SET segments_json=? WHERE id=?",
            (
                json.dumps(
                    [
                        {"type": "file", "data": {"artifact_id": "old", "version": "v1"}},
                        {"type": "image", "data": {"artifact_id": "old", "version": "v1"}},
                        {"type": "file", "data": {"artifact_id": "numeric", "version": 1}},
                        {"type": "file", "data": {"artifact_id": "missing"}},
                    ]
                ),
                event_id,
            ),
        )
        before = {
            table: connection.execute(f'SELECT * FROM "{table}" ORDER BY rowid').fetchall()
            for table in PROTECTED
        }
        session_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(plugin_agent_sessions)")
        }
        assert "allowed_capabilities_json" in session_columns
        kept_columns = sorted(session_columns - {"allowed_capabilities_json"})
        sessions_before = connection.execute(
            "SELECT " + ",".join(kept_columns) + " FROM plugin_agent_sessions"
        ).fetchall()
    command.upgrade(Config("alembic.ini"), "head")
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("PRAGMA foreign_keys=ON")
        for table, rows in before.items():
            assert connection.execute(f'SELECT * FROM "{table}" ORDER BY rowid').fetchall() == rows
        assert connection.execute("SELECT * FROM chat_event_artifact_refs").fetchall() == [
            (event_id, "old", "v1")
        ]
        assert {
            row[1] for row in connection.execute("PRAGMA table_info(plugin_agent_sessions)")
        } == session_columns - {"allowed_capabilities_json"}
        assert (
            connection.execute(
                "SELECT " + ",".join(kept_columns) + " FROM plugin_agent_sessions"
            ).fetchall()
            == sessions_before
        )
        for name, expected in ARTIFACT_REFERENCE_TRIGGERS.items():
            actual = connection.execute(
                "SELECT sql FROM sqlite_master WHERE name=?", (name,)
            ).fetchone()[0]
            assert " ".join(actual.split()) == " ".join(expected.split())
        connection.execute(
            "UPDATE chat_events SET segments_json=? WHERE id=?",
            (
                json.dumps([{"type": "file", "data": {"artifact_id": "old", "version": "v2"}}]),
                event_id,
            ),
        )
        assert connection.execute("SELECT * FROM chat_event_artifact_refs").fetchall() == [
            (event_id, "old", "v2")
        ]
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()
    asyncio.run(require_canonical_schema(url))
