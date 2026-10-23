"""Indexed mention resolution normalizes directory facts, never accepted effects."""

import asyncio
import json
import shutil
import sqlite3
from contextlib import closing
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from tests.unit.test_migration_0119 import _seed_0118

from yuki.persistence.schema_guard import require_canonical_schema

PROTECTED = (
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
)


@pytest.fixture(scope="module")
def original_template(tmp_path_factory):
    path = tmp_path_factory.mktemp("mention-original") / "0141.db"
    with pytest.MonkeyPatch.context() as patch:
        _seed_0118(path, patch)
        command.upgrade(Config("alembic.ini"), "0141")
    return path


@pytest.fixture
def original(original_template, tmp_path, monkeypatch):
    path = tmp_path / "mentions.db"
    shutil.copyfile(original_template, path)
    url = f"sqlite+aiosqlite:///{path}"
    monkeypatch.setenv("DATABASE_URL", url)
    return path, url


def _rows(connection, table, columns):
    return connection.execute(f'SELECT {columns} FROM "{table}" ORDER BY rowid').fetchall()


def _name(connection, account, stage, scope, name, first, last, *, kind="nickname"):
    return connection.execute(
        "INSERT INTO account_name_observations(account_id,assignment_id,space_binding_id,"
        "name,kind,first_seen_at,last_seen_at) VALUES(?,?,?,?,?,?,?) RETURNING id",
        (account, stage, scope, name, kind, first, last),
    ).fetchone()[0]


def test_upgrade_merges_only_identical_name_owners_and_preserves_original_facts(original):
    path, url = original
    with closing(sqlite3.connect(path)) as connection, connection:
        assignments = connection.execute(
            "SELECT id,account_id,person_id FROM account_assignments "
            "WHERE kind='person' ORDER BY id"
        ).fetchall()
        stage, account, person = assignments[0]
        other_stage, other_account, _ = assignments[1]
        space, scope, ended = (str(uuid4()) for _ in range(3))
        first, last = "2026-10-01 00:00:00", "2026-10-20 00:00:00"
        connection.execute(
            "INSERT INTO spaces(id,name,enabled,autonomous_enabled,require_mention,revision,"
            "created_at,updated_at) VALUES(?,'original group',1,0,0,1,?,?)",
            (space, first, last),
        )
        connection.execute(
            "INSERT INTO space_bindings(id,space_id,platform,external_space_id,display_name,"
            "status,revision,first_seen_at,last_seen_at,created_at,updated_at) "
            "VALUES(?,?,'qq','original-group','','active',1,?,?,?,?)",
            (scope, space, first, last, first, last),
        )
        connection.execute(
            "INSERT INTO account_assignments(id,account_id,kind,person_id,status,started_at,"
            "ended_at,first_seen_at,last_seen_at,created_at,updated_at,revision,ingest_eligible) "
            "VALUES(?,?,'person',?,'ended',?,?,?,?,?,?,1,1)",
            (ended, account, person, first, last, first, last, first, last),
        )
        keep = _name(connection, account, stage, None, "  Ice\t ", first, first)
        _name(connection, account, stage, None, "Ice", last, last)
        _name(connection, account, stage, None, "I\u200bce", first, last)
        independent = [
            _name(connection, account, None, None, " Ice ", first, last),
            _name(connection, account, ended, None, " Ice ", first, last),
            _name(connection, account, stage, scope, " Ice ", first, last),
            _name(connection, other_account, other_stage, None, " Ice ", first, last),
        ]
        _name(connection, account, stage, None, "\x00\u200b\n", first, last)
        # The shared display contract strips a space left by truncation too.
        long_name = "x" * 127
        long_ids = (
            _name(connection, account, stage, None, long_name + " more", first, last),
            _name(connection, account, stage, None, long_name + " ", first, last),
        )
        presence = connection.execute(
            "SELECT id FROM account_assignments WHERE kind='yuki' LIMIT 1"
        ).fetchone()[0]
        conversation = connection.execute(
            "SELECT id FROM canonical_conversations LIMIT 1"
        ).fetchone()[0]
        prepared = json.dumps(
            {"message": {"text": "@I\u200bce", "mentions": [{"account_id": account}]}},
            ensure_ascii=False,
            indent=2,
        )
        connection.execute(
            "INSERT INTO social_operation_receipts(id,source_turn_id,tool_call_id,"
            "source_conversation_id,action,payload_hash,target_kind,target_id,presence_id,"
            "status,created_at,updated_at,planned_parts,prepared_json) "
            "VALUES(?,'original-turn','original-call',?,'send_message',?,'person',?,?,'prepared',?,?,1,?)",
            (str(uuid4()), conversation, "a" * 64, person, presence, first, last, prepared),
        )
        # Compare every original field through head; newly added receipt source
        # columns are separate facts rather than mutations of the old payload.
        original_columns = {
            table: ",".join(
                f'"{row[1]}"' for row in connection.execute(f'PRAGMA table_info("{table}")')
            )
            for table in PROTECTED
        }
        before = {table: _rows(connection, table, original_columns[table]) for table in PROTECTED}
    command.upgrade(Config("alembic.ini"), "head")
    with closing(sqlite3.connect(path)) as connection:
        merged = connection.execute(
            "SELECT id,name,first_seen_at,last_seen_at FROM account_name_observations WHERE id=?",
            (keep,),
        ).fetchone()
        assert merged == (keep, "Ice", first, last)
        assert connection.execute(
            "SELECT id FROM account_name_observations WHERE account_id=? AND assignment_id=? "
            "AND space_binding_id IS NULL AND name='Ice'",
            (account, stage),
        ).fetchall() == [(keep,)]
        assert (
            connection.execute(
                "SELECT name FROM account_name_observations WHERE id IN (?,?,?,?) ORDER BY id",
                independent,
            ).fetchall()
            == [("Ice",)] * 4
        )
        assert connection.execute(
            "SELECT name FROM account_name_observations WHERE id IN (?,?) ORDER BY id", long_ids
        ).fetchall() == [(long_name,)]
        assert not connection.execute(
            "SELECT id FROM account_name_observations WHERE name=''"
        ).fetchall()
        for table in PROTECTED:
            assert _rows(connection, table, original_columns[table]) == before[table]
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed"):
            _name(connection, account, stage, None, "Ice", first, last)
    asyncio.run(require_canonical_schema(url))


@pytest.mark.parametrize(
    ("first", "last", "earlier", "later"),
    [
        (
            "2026-10-19T01:00:00",
            "2026-10-19T02:00:00",
            "2026-10-19 00:00:00",
            "2026-10-19 23:00:00",
        ),
        (
            "2026-10-19T01:00:00+00:00",
            "2026-10-19T23:00:00+00:00",
            "2026-10-19T08:00:00+09:00",
            "2026-10-19T19:00:00-05:00",
        ),
    ],
)
def test_name_merge_compares_instants_and_preserves_original_timestamp_bytes(
    original, first, last, earlier, later
):
    path, _ = original
    with closing(sqlite3.connect(path)) as connection, connection:
        stage, account = connection.execute(
            "SELECT id,account_id FROM account_assignments WHERE kind='person' LIMIT 1"
        ).fetchone()
        keep = _name(connection, account, stage, None, " Original ", first, last)
        _name(connection, account, stage, None, "Original", earlier, later)
    command.upgrade(Config("alembic.ini"), "head")
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute(
            "SELECT id,name,first_seen_at,last_seen_at FROM account_name_observations WHERE id=?",
            (keep,),
        ).fetchone() == (keep, "Original", earlier, later)
