"""Indexed mention resolution normalizes directory facts, never accepted effects."""

import asyncio
import json
import runpy
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from tests.unit.test_migration_0119 import _seed_0118

from yuki.domain.messages import sanitize_display_name
from yuki.persistence.schema_guard import require_canonical_schema

NAME_INDEX = "ix_account_names_name_scope_account"
MESSAGE_INDEX = "ix_chat_events_conversation_message_sender_time"
OLD_MESSAGE_INDEX = "ix_chat_events_conversation_sender_time"
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
        before = {
            table: _rows(connection, table, original_columns[table]) for table in PROTECTED
        }
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


def test_new_indexes_use_exact_names_and_skip_external_events(original):
    path, _ = original
    command.upgrade(Config("alembic.ini"), "head")
    with closing(sqlite3.connect(path)) as connection:
        names = {row[1] for row in connection.execute("PRAGMA index_list(chat_events)")}
        assert MESSAGE_INDEX in names and OLD_MESSAGE_INDEX not in names
        for scope_condition in ("space_binding_id IS NULL", "space_binding_id=?"):
            params = ("Ice",) if "NULL" in scope_condition else ("Ice", str(uuid4()))
            plan = connection.execute(
                "EXPLAIN QUERY PLAN SELECT account_id FROM account_name_observations "
                f"WHERE name=? AND {scope_condition}",
                params,
            ).fetchall()
            assert any(NAME_INDEX in row[3] for row in plan), plan
            assert not any("SCAN" in row[3] for row in plan), plan
        plan = connection.execute(
            "EXPLAIN QUERY PLAN SELECT id,occurred_at FROM chat_events "
            "WHERE canonical_conversation_id=? AND sender_user_id=? "
            "AND event_kind='message' AND suppression_status='keeper' "
            "ORDER BY occurred_at DESC,id DESC LIMIT 1",
            ("original-conversation", "original-sender"),
        ).fetchall()
        assert any("COVERING INDEX " + MESSAGE_INDEX in row[3] for row in plan), plan
        assert not any("SCAN" in row[3] or "TEMP B-TREE" in row[3] for row in plan), plan


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


def test_recent_lookup_does_not_walk_newer_external_events(original):
    path, _ = original
    command.upgrade(Config("alembic.ini"), "head")
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.row_factory = sqlite3.Row
        seed = dict(connection.execute("SELECT * FROM chat_events LIMIT 1").fetchone())
        presence = connection.execute(
            "SELECT id FROM account_assignments WHERE kind='yuki' LIMIT 1"
        ).fetchone()[0]
        seed.update(
            id=100,
            sender_user_id=seed["bot_user_id"],
            direction="outbound",
            author_kind="yuki",
            author_person_id=None,
            author_presence_id=presence,
            author_account_id=None,
            ingress_presence_id=presence,
            occurred_at="2026-10-19 00:00:00",
            canonical_event_id=str(uuid4()),
        )
        columns = tuple(seed)
        insert = (
            "INSERT INTO chat_events ("
            + ",".join(columns)
            + ") VALUES("
            + ",".join("?" for _ in columns)
            + ")"
        )
        connection.execute(insert, tuple(seed.values()))
        query = (
            "SELECT id FROM chat_events WHERE canonical_conversation_id=? AND sender_user_id=? "
            "AND suppression_status='keeper' AND event_kind='message' "
            "ORDER BY occurred_at DESC,id DESC LIMIT 1"
        )
        parameters = (seed["canonical_conversation_id"], seed["sender_user_id"])

        def steps():
            connection.execute(query, parameters).fetchone()
            visited = []
            connection.set_progress_handler(lambda: visited.append(None) or 0, 1)
            try:
                assert connection.execute(query, parameters).fetchone()[0] == seed["id"]
            finally:
                connection.set_progress_handler(None, 0)
            return len(visited)

        before = steps()
        external = dict(
            seed,
            direction="external",
            event_kind="external_event",
            author_kind="system",
            author_presence_id=None,
            origin="plugin_background",
            source_plugin_id="github-monitor",
            external_source="github",
            external_event_type="PushEvent",
            external_payload_json="{}",
            external_target_id=seed["private_peer_user_id"],
            occurred_at="2026-10-20 00:00:00",
        )
        values = []
        for index in range(1000):
            external.update(
                id=101 + index,
                canonical_event_id=str(uuid4()),
                external_event_key=f"original-external:{index}",
            )
            values.append(tuple(external[column] for column in columns))
        connection.executemany(insert, values)
        # This checks bounded indexed work rather than an environment-specific
        # latency threshold; external rows must never enter the partial index.
        assert steps() == before


@pytest.mark.parametrize(
    "value",
    ["  Ice\t ", "I\u200bce", "A\nB", "A\u2028B", "x" * 127 + " more", "\x00\u200b\n"],
)
def test_frozen_name_normalization_matches_current_display_contract(value):
    migration = runpy.run_path(str(Path("migrations/versions/0142_indexed_mentions.py")))
    assert migration["_normalized_name"](value) == sanitize_display_name(value)
