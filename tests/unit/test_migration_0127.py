"""The frozen target upgrade uses original internal evidence, never current accounts."""

import importlib.util
import json
from pathlib import Path
from uuid import uuid4

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine


def test_0127_preserves_claims_and_only_backfills_proven_original_targets(tmp_path):
    spec = importlib.util.spec_from_file_location(
        "rebuild_target_upgrade",
        Path(__file__).parents[2] / "migrations/versions/0127_rebuild_canonical_targets.py",
    )
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    author, target, current_owner, space = (str(uuid4()) for _ in range(4))
    engine = create_engine(f"sqlite:///{tmp_path / 'upgrade.db'}")
    with engine.begin() as connection:
        for statement in (
            "CREATE TABLE persons(id TEXT PRIMARY KEY)",
            "CREATE TABLE spaces(id TEXT PRIMARY KEY)",
            "CREATE TABLE canonical_conversations(id TEXT PRIMARY KEY,kind TEXT,space_id TEXT)",
            "CREATE TABLE chat_events(id INTEGER PRIMARY KEY,author_person_id TEXT,"
            "canonical_conversation_id TEXT,reply_to_event_id INTEGER)",
            "CREATE TABLE memory_facts(id INTEGER PRIMARY KEY,canonical_subject_person_id TEXT,"
            "canonical_subject_space_id TEXT)",
            "CREATE TABLE identity_bindings(external_account_id TEXT,person_id TEXT)",
            "CREATE TABLE memory_rebuild_proposals(id INTEGER PRIMARY KEY,event_id INTEGER,"
            "scope_type TEXT,claim_json TEXT,actual_fact_id INTEGER,subject_user_id TEXT,"
            "claim_hash TEXT,commit_status TEXT)",
        ):
            connection.exec_driver_sql(statement)
        connection.exec_driver_sql(
            "INSERT INTO persons VALUES(?),(?),(?)", (author, target, current_owner)
        )
        connection.exec_driver_sql("INSERT INTO spaces VALUES(?)", (space,))
        connection.exec_driver_sql(
            "INSERT INTO canonical_conversations VALUES('group','space',?)", (space,)
        )
        connection.exec_driver_sql(
            "INSERT INTO chat_events VALUES(1,?,'group',2),(2,?,'group',NULL)", (author, target)
        )
        connection.exec_driver_sql("INSERT INTO memory_facts VALUES(1,?,?)", (target, space))
        connection.exec_driver_sql(
            "INSERT INTO identity_bindings VALUES('1001',?)", (current_owner,)
        )
        for index, (subject, scope, fact_id) in enumerate(
            (
                ("speaker", "person", None),
                (f"member_{target}", "person_group", None),
                ("reply_author", "person_group", None),
                ("named_member", "person_group", None),
                ("named_member", "person_group", 1),
                ("group", "group", None),
            ),
            start=1,
        ):
            connection.exec_driver_sql(
                "INSERT INTO memory_rebuild_proposals VALUES(?,1,?,?,?,?,?,'committed')",
                (
                    index,
                    scope,
                    json.dumps({"subject_ref": subject, "original": index}),
                    fact_id,
                    "1001",
                    f"hash-{index}",
                ),
            )
        original = connection.exec_driver_sql(
            "SELECT * FROM memory_rebuild_proposals ORDER BY id"
        ).all()
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()
        after = connection.exec_driver_sql(
            "SELECT * FROM memory_rebuild_proposals ORDER BY id"
        ).all()
        assert [tuple(row[:-2]) for row in after] == [tuple(row) for row in original]
        assert [tuple(row[-2:]) for row in after] == [
            (author, None),
            (target, space),
            (target, space),
            (None, space),
            (target, space),
            (None, space),
        ]
        assert not connection.exec_driver_sql("PRAGMA foreign_key_check").all()
        migration.downgrade()
        assert (
            connection.exec_driver_sql("SELECT * FROM memory_rebuild_proposals ORDER BY id").all()
            == original
        )
    engine.dispose()
