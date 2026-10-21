"""Full upgrade preserves original evidence and typed identity constraints."""

import asyncio
import json
import runpy
import sqlite3
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from tests.unit.test_migration_0119 import _seed_0118

from yuki.persistence.schema_guard import require_canonical_schema


def test_native_snapshot_delta_keeps_fts_audio_rowids_and_original_bytes():
    migration = runpy.run_path(
        str(Path(__file__).parents[2] / "migrations/versions/0141_account_assignment.py")
    )
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE accounts(id TEXT PRIMARY KEY, platform TEXT, external_account_"
            "id TEXT, display_name TEXT, created_at TEXT, updated_at TEXT)"
        )
        connection.exec_driver_sql(
            "CREATE TABLE chat_events(id INTEGER PRIMARY KEY, content TEXT, segments_jso"
            "n TEXT, scope_type TEXT, observed_at TEXT, bot_user_id TEXT, event_kind TEX"
            "T, audio_transcript TEXT)"
        )
        connection.exec_driver_sql(
            "CREATE VIRTUAL TABLE chat_events_fts USING fts5(content,audio_transcript,co"
            "ntent='chat_events',content_rowid='id',tokenize='trigram')"
        )
        segments = json.dumps(
            [
                {"type": "text", "data": {"text": "hello "}},
                {"type": "at", "data": {"qq": "123", "name": "TargetAlice"}},
            ]
        )
        connection.execute(
            text(
                "INSERT INTO chat_events VALUES (17,'hello [提及123]',:segments,'group','2026-"
                "10-18','999','message','SpeechTranscript'),(18,'UntouchedBody','[]','group'"
                ",'2026-10-18','999','message','OtherTranscript')"
            ),
            {"segments": segments},
        )
        connection.exec_driver_sql("INSERT INTO chat_events_fts(chat_events_fts) VALUES('rebuild')")
        assert migration["normalize_native_snapshots"](connection) == {
            "native_snapshots": 1,
            "unlocated_event_ids": [],
        }
        changed = connection.exec_driver_sql(
            "SELECT content,segments_json,audio_transcript FROM chat_events WHERE id=17"
        ).one()
        assert changed[0] == "hello @TargetAlice" and changed[2] == "SpeechTranscript"
        context = json.loads(changed[1])[-1]["data"]
        assert context["original_segments_json"] == segments
        assert context["original_content"] == "hello [提及123]"
        for word, expected in (
            ("TargetAlice", [17]),
            ("提及123", []),
            ("SpeechTranscript", [17]),
            ("UntouchedBody", [18]),
            ("OtherTranscript", [18]),
        ):
            assert (
                list(
                    connection.execute(
                        text("SELECT rowid FROM chat_events_fts WHERE chat_events_fts MATCH :word"),
                        {"word": word},
                    ).scalars()
                )
                == expected
            )
        connection.exec_driver_sql(
            "INSERT INTO chat_events_fts(chat_events_fts,rank) VALUES('integrity-check',1)"
        )
    engine.dispose()


@pytest.mark.parametrize("existing_alias", [False, True])
def test_full_0140_upgrade_keeps_original_ids_paid_sources_and_typed_constraints(
    tmp_path, monkeypatch, existing_alias
):
    path = tmp_path / "cutover.db"
    url, _ = _seed_0118(path, monkeypatch)
    config = Config("alembic.ini")
    command.upgrade(config, "0140")
    protected = (
        "runtime_work",
        "runtime_work_effects",
        "client_devices",
        "control_command_receipts",
        "prompt_projection_chunks",
    )
    with sqlite3.connect(path) as connection:
        original = {
            name: connection.execute("SELECT * FROM " + name).fetchall() for name in protected
        }
        ids = {
            row[0]
            for row in connection.execute(
                "SELECT id FROM identity_bindings UNION ALL SELECT id FROM presences"
            )
        }
        presence = connection.execute("SELECT id FROM presences LIMIT 1").fetchone()[0]
        event = connection.execute(
            "SELECT canonical_event_id,observed_at FROM chat_events WHERE id=2"
        ).fetchone()
        person = connection.execute("SELECT id FROM persons LIMIT 1").fetchone()[0]
        space = str(uuid4())
        connection.execute(
            "INSERT INTO spaces(id,name,enabled,autonomous_enabled,require_mention,"
            "revision,created_at,updated_at) VALUES(?,'original group',1,0,0,1,?,?)",
            (space, event[1], event[1]),
        )
        old_card = "无账号证明的原人物群名称"
        connection.execute(
            "INSERT INTO memberships(canonical_person_id,canonical_space_id,group_card,"
            "first_seen_at,last_seen_at) VALUES(?,?,?,?,?) "
            "ON CONFLICT(canonical_person_id,canonical_space_id) DO UPDATE SET "
            "group_card=excluded.group_card,first_seen_at=excluded.first_seen_at,"
            "last_seen_at=excluded.last_seen_at",
            (person, space, old_card, event[1], event[1]),
        )
        if existing_alias:
            connection.execute(
                "INSERT INTO person_aliases(canonical_person_id,canonical_space_id,alias,"
                "alias_type,first_seen_at,last_seen_at) VALUES(?,?,?,'group_card',?,?)",
                (person, space, old_card, event[1], event[1]),
            )
        connection.execute(
            "INSERT INTO canonical_event_receipts(ingress_presence_id,event_type,platfor"
            "m_message_id,canonical_event_id,created_at,observed_at) VALUES(?,'message',"
            "'original-key',?,?,?)",
            (presence, event[0], event[1], event[1]),
        )
        receipt_id = connection.execute("SELECT id FROM canonical_event_receipts").fetchone()[0]
    command.upgrade(config, "0141")
    with sqlite3.connect(path) as connection:
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()
        assert not connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('identity_bi"
            "ndings','presences')"
        ).fetchall()
        assert {r[0] for r in connection.execute("SELECT id FROM account_assignments")} == ids
        assert "group_card" not in {
            row[1] for row in connection.execute("PRAGMA table_info(memberships)")
        }
        assert connection.execute(
            "SELECT first_seen_at,last_seen_at FROM person_aliases "
            "WHERE canonical_person_id=? AND canonical_space_id=? AND alias=?",
            (person, space, old_card),
        ).fetchall() == [(event[1], event[1])]
        assert connection.execute(
            "SELECT count(*) FROM account_name_observations WHERE name=?", (old_card,)
        ).fetchone() == (0,)
        for table in protected:
            assert connection.execute("SELECT * FROM " + table).fetchall() == original[table]
        receipt = connection.execute(
            "SELECT id,ingress_presence_id,ingress_account_id,transport_namespace FROM c"
            "anonical_event_receipts"
        ).fetchone()
        assert receipt[:2] == (receipt_id, presence)
        assert (
            receipt[2]
            == connection.execute(
                "SELECT account_id FROM account_assignments WHERE id=?", (presence,)
            ).fetchone()[0]
        )
        assert json.loads(receipt[3]) == ["private", "0", None]
        human = connection.execute(
            "SELECT id FROM account_assignments WHERE kind='person' LIMIT 1"
        ).fetchone()[0]
        with pytest.raises(sqlite3.IntegrityError, match="assignment kind mismatch"):
            connection.execute(
                "UPDATE canonical_event_receipts SET ingress_presence_id=?", (human,)
            )
        connection.rollback()
        connection.execute("UPDATE account_assignments SET status='ended' WHERE id=?", (presence,))
        with pytest.raises(sqlite3.IntegrityError, match="ended assignment"):
            connection.execute(
                "UPDATE account_assignments SET status='active' WHERE id=?", (presence,)
            )
    command.upgrade(config, "head")
    asyncio.run(require_canonical_schema(url))
