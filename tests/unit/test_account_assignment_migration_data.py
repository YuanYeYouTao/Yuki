"""Frozen 0141 transforms keep paid facts and the old selector's exact snapshot."""

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text


@pytest.fixture
def migrated_data():
    path = Path("migrations/versions/0141_account_assignment.py")
    spec = importlib.util.spec_from_file_location("migration_0141_work_data", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        for ddl in (
            "CREATE TABLE runtime_work_journal(work_id TEXT PRIMARY KEY,chain_id TEXT,"
            "contract TEXT,source_revision INTEGER,phase TEXT,payload_json TEXT,"
            "updated REAL,archived_journal_json TEXT)",
            "CREATE TABLE memory_rebuild_runs(id TEXT PRIMARY KEY,selection_json TEXT,"
            "selection_hash TEXT,snapshot_max_event_id INTEGER,original_selection_json TEXT,"
            "original_selection_hash TEXT,status TEXT,last_event_id INTEGER,"
            "processed_events INTEGER)",
            "CREATE TABLE chat_events(id INTEGER PRIMARY KEY,platform TEXT,bot_user_id TEXT,"
            "sender_user_id TEXT,group_id TEXT,scope_type TEXT,occurred_at TEXT)",
            "CREATE TABLE immutable_evidence(kind TEXT,body TEXT)",
        ):
            connection.execute(text(ddl))
        rows = (
            (1, "qq", "bot", "same", "group", "group", "2026-10-19 00:00:00.000000"),
            (2, "telegram", "bot", "same", "group", "group", "2026-10-19 01:00:00.000000"),
            (3, "qq", "other-bot", "same", "group", "group", "2026-10-19 02:00:00.000000"),
            (4, "qq", "bot", "other-person", "group", "group", "2026-10-19 03:00:00.000000"),
            (5, "qq", "bot", "same", None, "private", "2026-10-19 04:00:00.000000"),
            (6, "qq", "bot", "same", "group", "group", "2026-10-19 05:00:00.000000"),
        )
        for row in rows:
            connection.exec_driver_sql("INSERT INTO chat_events VALUES(?,?,?,?,?,?,?)", row)
        for kind in (
            "work-source-budget-parent-external-run",
            "paid-extraction-fingerprint-item",
            "protocol-media-ref",
            "social-effect-receipt",
        ):
            connection.execute(
                text("INSERT INTO immutable_evidence VALUES(:kind,:body)"),
                {"kind": kind, "body": json.dumps({"original": kind, "budget": 13})},
            )
        yield connection, module.migrate_work_and_rebuild
    engine.dispose()


def seed_run(connection, selection, *, snapshot=5):
    body = json.dumps(selection, ensure_ascii=False, indent=2)
    digest = hashlib.sha256(body.encode()).hexdigest()
    connection.execute(
        text(
            "INSERT INTO memory_rebuild_runs VALUES("
            "'run',:body,:digest,:snapshot,NULL,NULL,'running',2,1)"
        ),
        {"body": body, "digest": digest, "snapshot": snapshot},
    )
    return body, digest


def run_row(connection):
    return dict(connection.execute(text("SELECT * FROM memory_rebuild_runs")).mappings().one())


def test_private_journal_archive_keeps_original_bytes_and_unrelated_paid_facts(migrated_data):
    connection, migrate = migrated_data
    original = {
        "work_id": "original-work",
        "chain_id": "original-chain",
        "contract": "retired-contract",
        "source_revision": 12,
        "phase": "dispatched",
        "payload_json": '{ "opaque": "原始付费内容", "pending": ["original-call"] }',
        "updated": 123.5,
    }
    connection.execute(
        text(
            "INSERT INTO runtime_work_journal VALUES("
            ":work_id,:chain_id,:contract,:source_revision,:phase,:payload_json,:updated,NULL)"
        ),
        original,
    )
    untouched = connection.execute(text("SELECT * FROM immutable_evidence")).all()
    assert migrate(connection) == {"journal_archived": 1, "rebuild_frozen": 0}
    saved = dict(connection.execute(text("SELECT * FROM runtime_work_journal")).mappings().one())
    assert json.loads(saved.pop("archived_journal_json")) == original
    assert saved == original
    assert connection.execute(text("SELECT * FROM immutable_evidence")).all() == untouched
    assert migrate(connection) == {"journal_archived": 0, "rebuild_frozen": 0}


@pytest.mark.parametrize(
    "selection,expected",
    [
        ({"bot_user_ids": ["bot"], "sender_user_ids": ["same"], "group_ids": ["group"]}, [1, 2]),
        ({"scope_types": ["private"]}, [5]),
        ({"sender_user_ids": ["missing"]}, []),
        (
            {
                "minimum_event_id": 2,
                "maximum_event_id": 5,
                "after": "2026-10-19T09:00:00+08:00",
                "before": "2026-10-19T11:00:00+08:00",
            },
            [2, 3, 4],
        ),
    ],
)
def test_original_selector_freezes_real_event_ids_without_today_account_resolution(
    migrated_data, selection, expected
):
    connection, migrate = migrated_data
    selection = {**selection, "maximum_events": 1, "include_bot_messages": False}
    original, digest = seed_run(connection, selection)
    paid = connection.execute(text("SELECT * FROM immutable_evidence")).all()
    assert migrate(connection) == {"journal_archived": 0, "rebuild_frozen": 1}
    saved = run_row(connection)
    current = json.loads(saved["selection_json"])
    assert current["event_ids"] == expected
    assert all(
        current[name] == []
        for name in ("presence_ids", "person_ids", "space_ids", "conversation_ids")
    )
    assert not {"bot_user_ids", "sender_user_ids", "group_ids"}.intersection(current)
    assert current["maximum_events"] == 1 and current["include_bot_messages"] is False
    assert saved["original_selection_json"] == original
    assert saved["original_selection_hash"] == digest
    assert saved["selection_hash"] == hashlib.sha256(saved["selection_json"].encode()).hexdigest()
    assert (saved["status"], saved["last_event_id"], saved["processed_events"]) == ("running", 2, 1)
    assert connection.execute(text("SELECT * FROM immutable_evidence")).all() == paid
    assert migrate(connection) == {"journal_archived": 0, "rebuild_frozen": 0}
    assert run_row(connection) == saved


def test_empty_native_filter_remains_unfiltered_inside_original_snapshot(migrated_data):
    connection, migrate = migrated_data
    seed_run(connection, {"bot_user_ids": [], "sender_user_ids": [], "group_ids": []}, snapshot=2)
    migrate(connection)
    assert json.loads(run_row(connection)["selection_json"])["event_ids"] == [1, 2]


def test_events_arriving_after_frozen_run_are_never_added_on_second_upgrade(migrated_data):
    connection, migrate = migrated_data
    seed_run(connection, {}, snapshot=2)
    migrate(connection)
    before = run_row(connection)
    connection.execute(
        text(
            "INSERT INTO chat_events VALUES("
            "7,'qq','bot','same','group','group','2026-10-19 00:00:00.000000')"
        )
    )
    migrate(connection)
    assert run_row(connection) == before
    assert json.loads(before["selection_json"])["event_ids"] == [1, 2]
