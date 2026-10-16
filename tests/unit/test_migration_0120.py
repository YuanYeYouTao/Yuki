"""0120 retains historical IDs and aborts rather than guessing a private owner."""

import sqlite3
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from tests.unit.test_migration_0119 import _seed_0118


def test_0120_retains_history_work_receipts_and_primary_alias(tmp_path, monkeypatch):
    path = tmp_path / "split.db"
    _, ids = _seed_0118(path, monkeypatch)
    config = Config("alembic.ini")
    command.upgrade(config, "0119")
    with sqlite3.connect(path) as connection:
        original = connection.execute(
            "SELECT id,person_id,primary_alias_id,generation,prompt_source_revision,"
            "starts_after_event_id,last_event_id,revision FROM canonical_conversations ORDER BY id"
        ).fetchall()
        work = connection.execute("SELECT * FROM runtime_work ORDER BY id").fetchall()
        events = connection.execute("SELECT * FROM chat_events ORDER BY id").fetchall()
        checkpoints = connection.execute(
            "SELECT * FROM prompt_projections ORDER BY view_key"
        ).fetchall()
        person = connection.execute(
            "SELECT person_id FROM canonical_conversations WHERE id=?", (ids[0],)
        ).fetchone()[0]
        now = "2026-10-14 00:00:00"
        second = str(uuid4())
        connection.execute(
            "INSERT INTO identity_bindings VALUES(?,?,'qq','second-account','','active',1,?,?,?,?)",
            (second, person, now, now, now, now),
        )
        connection.execute(
            "INSERT INTO conversation_legacy_aliases VALUES(?,?,?,0,?,?)",
            (str(uuid4()), ids[0], "bot:test:private:second-account", now, now),
        )
    command.upgrade(config, "0120")
    with sqlite3.connect(path) as connection:
        assert (
            connection.execute(
                "SELECT id,person_id,primary_alias_id,generation,prompt_source_revision,"
                "starts_after_event_id,last_event_id,revision "
                "FROM canonical_conversations ORDER BY id"
            ).fetchall()
            == original
        )
        assert connection.execute("SELECT * FROM runtime_work ORDER BY id").fetchall() == work
        assert connection.execute("SELECT * FROM chat_events ORDER BY id").fetchall() == events
        assert (
            connection.execute("SELECT * FROM prompt_projections ORDER BY view_key").fetchall()
            == checkpoints
        )
        assert connection.execute(
            "SELECT count(*) FROM canonical_conversations WHERE identity_binding_id=?", (second,)
        ).fetchone() == (0,)
        for conversation in ids:
            assert connection.execute(
                "SELECT b.external_account_id FROM canonical_conversations c "
                "JOIN identity_bindings b "
                "ON b.id=c.identity_binding_id WHERE c.id=?",
                (conversation,),
            ).fetchone() == (str(ids.index(conversation)),)
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()


def test_0120_unresolved_primary_aborts_without_partial_changes(tmp_path, monkeypatch):
    path = tmp_path / "unresolved.db"
    _, ids = _seed_0118(path, monkeypatch)
    config = Config("alembic.ini")
    command.upgrade(config, "0119")
    with sqlite3.connect(path) as connection:
        connection.execute("DELETE FROM identity_bindings WHERE external_account_id='0'")
    with pytest.raises(RuntimeError, match=ids[0]):
        command.upgrade(config, "0120")
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == ("0119",)
        assert "identity_binding_id" not in {
            row[1] for row in connection.execute("PRAGMA table_info(canonical_conversations)")
        }


def _new_0120_private(connection, person=None):
    now = "2026-10-15 00:00:00"
    if person is None:
        person = str(uuid4())
        connection.execute("INSERT INTO persons VALUES(?,1,1,?,?)", (person, now, now))
    binding, conversation, alias = (str(uuid4()) for _ in range(3))
    connection.execute(
        "INSERT INTO identity_bindings VALUES(?,?,'qq',?,'','active',1,?,?,?,?)",
        (binding, person, binding, now, now, now, now),
    )
    connection.execute(
        "INSERT INTO canonical_conversations(id,kind,person_id,identity_binding_id,"
        "primary_alias_id,generation,starts_after_event_id,last_event_id,"
        "last_generation_change_event_id,revision,created_at,updated_at) "
        "VALUES(?,'private',?,?,?,1,0,0,0,1,?,?)",
        (conversation, person, binding, alias, now, now),
    )
    connection.execute(
        "INSERT INTO conversation_legacy_aliases VALUES(?,?,?,1,?,?)",
        (alias, conversation, f"binding:{binding}", now, now),
    )
    return person, binding, conversation


def test_0120_unsplit_schema_roundtrip_and_split_history_refuses_downgrade(tmp_path, monkeypatch):
    path = tmp_path / "roundtrip.db"
    _seed_0118(path, monkeypatch)
    config = Config("alembic.ini")
    command.upgrade(config, "0120")
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        person, binding, conversation = _new_0120_private(connection)
    command.downgrade(config, "0119")
    command.upgrade(config, "0120")
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT person_id,identity_binding_id FROM canonical_conversations WHERE id=?",
            (conversation,),
        ).fetchone() == (person, binding)
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()
        connection.execute("PRAGMA foreign_keys=ON")
        _new_0120_private(connection, person)
    with pytest.raises(RuntimeError, match=person):
        command.downgrade(config, "0119")
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == ("0120",)
        assert connection.execute(
            "SELECT count(*) FROM canonical_conversations WHERE person_id=?", (person,)
        ).fetchone() == (2,)
