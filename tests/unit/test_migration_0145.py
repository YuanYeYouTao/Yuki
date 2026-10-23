"""Offline history replacement releases its writer and never publishes a partial build."""

import asyncio
import shutil
import sqlite3
from contextlib import closing
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import event
from sqlalchemy.engine import Engine
from tests.unit.test_migration_0119 import _seed_0118

from yuki.persistence.history_search import compile_match, prepare_query
from yuki.persistence.schema_guard import (
    CanonicalSchemaError,
    canonical_schema_revision,
    require_canonical_schema,
)


@pytest.fixture(scope="module")
def history_template(tmp_path_factory):
    path = tmp_path_factory.mktemp("history-upgrade") / "0143.db"
    with pytest.MonkeyPatch.context() as patch:
        _seed_0118(path, patch)
        command.upgrade(Config("alembic.ini"), "0143")
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("UPDATE chat_events SET content='繁體乾坤' WHERE id=2")
        original = connection.execute("SELECT * FROM chat_events WHERE id=2").fetchone()
        columns = [row[1] for row in connection.execute("PRAGMA table_info(chat_events)")]
        for index in range(270):
            row = dict(zip(columns, original, strict=True))
            row.update(
                id=10 + index,
                platform_message_id=f"backfill:{index}",
                canonical_event_id=str(uuid4()),
            )
            connection.execute(
                "INSERT INTO chat_events("
                + ",".join(columns)
                + ") VALUES("
                + ",".join("?" for _ in columns)
                + ")",
                tuple(row[name] for name in columns),
            )
    return path


@pytest.mark.parametrize("target", ["head", "heads", "0145"])
def test_upgrade_backfills_before_head_without_conversion_under_writer(
    history_template, tmp_path, monkeypatch, target
):
    import yuki.persistence.history_search_index as module

    path = tmp_path / "upgrade.db"
    shutil.copyfile(history_template, path)
    url = f"sqlite+aiosqlite:///{path}"
    monkeypatch.setenv("DATABASE_URL", url)
    original = module.prepare_terms
    calls = 0

    def prepare(*args):
        nonlocal calls
        calls += 1
        with closing(sqlite3.connect(path, timeout=0.05)) as concurrent:
            concurrent.execute("BEGIN IMMEDIATE")
            concurrent.rollback()
        return original(*args)

    monkeypatch.setattr(module, "prepare_terms", prepare)
    with closing(sqlite3.connect(path)) as connection:
        before = connection.execute("SELECT * FROM chat_events ORDER BY id").fetchall()
    command.upgrade(Config("alembic.ini"), target)
    assert calls == len(before)
    with closing(sqlite3.connect(path)) as connection, connection:
        expected_revision = "0145" if target == "0145" else canonical_schema_revision()
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
            expected_revision,
        )
        assert connection.execute("SELECT * FROM chat_events ORDER BY id").fetchall() == before
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master WHERE name='chat_events_fts'"
            ).fetchone()
            is None
        )
        conversation_id = connection.execute(
            "SELECT canonical_conversation_id FROM chat_events WHERE id=2"
        ).fetchone()[0]
        assert connection.execute(
            "SELECT rowid FROM chat_history_fts WHERE chat_history_fts MATCH ? ORDER BY rowid",
            (compile_match(conversation_id, prepare_query("繁体")),),
        ).fetchall() == [(2,), *[(10 + index,) for index in range(270)]]
        connection.execute("DELETE FROM chat_events WHERE id=2")
        assert (
            connection.execute("SELECT rowid FROM chat_history_fts WHERE rowid=2").fetchone()
            is None
        )
    calls = 0
    command.upgrade(Config("alembic.ini"), "head")
    assert calls == 0
    asyncio.run(require_canonical_schema(url))


def test_interrupted_partial_load_stays_0144_and_rebuilds_on_resume(
    history_template, tmp_path, monkeypatch
):
    import yuki.persistence.history_search_index as module

    path = tmp_path / "interrupted.db"
    shutil.copyfile(history_template, path)
    url = f"sqlite+aiosqlite:///{path}"
    monkeypatch.setenv("DATABASE_URL", url)
    original = module.prepare_terms
    calls = 0

    def interrupt(*args):
        nonlocal calls
        calls += 1
        if calls == 258:
            raise RuntimeError("interrupted conversion")
        return original(*args)

    monkeypatch.setattr(module, "prepare_terms", interrupt)
    with pytest.raises(RuntimeError, match="interrupted conversion"):
        command.upgrade(Config("alembic.ini"), "head")
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == ("0144",)
        assert 0 < connection.execute("SELECT count(*) FROM chat_history_fts").fetchone()[0] < 274
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE name='chat_events_fts'"
        ).fetchone()
    with pytest.raises(CanonicalSchemaError, match="migration head"):
        asyncio.run(require_canonical_schema(url))
    monkeypatch.setattr(module, "prepare_terms", original)
    command.upgrade(Config("alembic.ini"), "0145")
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == ("0145",)
        assert connection.execute("SELECT count(*) FROM chat_history_fts").fetchone() == (274,)
    command.upgrade(Config("alembic.ini"), "head")
    asyncio.run(require_canonical_schema(url))


def test_sql_only_upgrade_cannot_publish_unconverted_index(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'unused.db'}")
    with pytest.raises(RuntimeError, match="--sql cannot convert source text"):
        command.upgrade(Config("alembic.ini"), "0143:0145", sql=True)


@pytest.mark.parametrize(
    ("statement", "revision", "exists"),
    [
        ("CREATE INDEX ix_chat_events_conversation_occurred", "0143", False),
        ("DROP TABLE chat_events_fts", "0144", True),
    ],
)
def test_structural_and_final_interruptions_preserve_sources_and_resume(
    history_template, tmp_path, monkeypatch, statement, revision, exists
):
    path = tmp_path / "boundary.db"
    shutil.copyfile(history_template, path)
    url = f"sqlite+aiosqlite:///{path}"
    monkeypatch.setenv("DATABASE_URL", url)
    with closing(sqlite3.connect(path)) as connection:
        before = connection.execute("SELECT * FROM chat_events ORDER BY id").fetchall()

    def interrupt(_connection, _cursor, sql, _parameters, _context, _executemany):
        if sql.startswith(statement):
            raise RuntimeError("interrupted structure")

    event.listen(Engine, "before_cursor_execute", interrupt)
    try:
        with pytest.raises(RuntimeError, match="interrupted structure"):
            command.upgrade(Config("alembic.ini"), "head")
    finally:
        event.remove(Engine, "before_cursor_execute", interrupt)
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
            revision,
        )
        assert connection.execute("SELECT * FROM chat_events ORDER BY id").fetchall() == before
        assert (
            bool(
                connection.execute(
                    "SELECT name FROM sqlite_master WHERE name='chat_history_fts'"
                ).fetchone()
            )
            == exists
        )
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE name='chat_events_fts'"
        ).fetchone()
    command.upgrade(Config("alembic.ini"), "head")
    asyncio.run(require_canonical_schema(url))


def test_upgrade_keeps_proven_high_water_and_never_reuses_deleted_ids(
    history_template, tmp_path, monkeypatch
):
    path = tmp_path / "monotonic.db"
    shutil.copyfile(history_template, path)
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{path}")
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            "UPDATE canonical_conversations SET last_event_id=1000,"
            "starts_after_event_id=990,last_generation_change_event_id=995"
        )
        columns = [row[1] for row in connection.execute("PRAGMA table_info(chat_events)")]
        original = dict(
            zip(
                columns,
                connection.execute("SELECT * FROM chat_events WHERE id=2").fetchone(),
                strict=True,
            )
        )
        foreign_keys = connection.execute("PRAGMA foreign_key_list(chat_events)").fetchall()
    command.upgrade(Config("alembic.ini"), "head")
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("PRAGMA foreign_keys=ON")
        assert (
            "AUTOINCREMENT"
            in connection.execute(
                "SELECT sql FROM sqlite_master WHERE name='chat_events'"
            ).fetchone()[0]
        )
        assert {row[2:] for row in connection.execute("PRAGMA foreign_key_list(chat_events)")} == {
            row[2:] for row in foreign_keys
        }
        columns.remove("id")

        def insert():
            original.update(platform_message_id=str(uuid4()), canonical_event_id=str(uuid4()))
            return connection.execute(
                "INSERT INTO chat_events("
                + ",".join(columns)
                + ") VALUES("
                + ",".join("?" for _ in columns)
                + ") RETURNING id",
                tuple(original[name] for name in columns),
            ).fetchone()[0]

        first = insert()
        assert first > 1000
        connection.execute("DELETE FROM chat_events WHERE id=?", (first,))
        second = insert()
        assert second > first
        connection.execute("DELETE FROM chat_events")
        assert insert() > second
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()


def test_upgrade_preserves_known_receipt_anchors_without_inventing_unknown_ones(
    history_template, tmp_path, monkeypatch
):
    path = tmp_path / "receipt.db"
    shutil.copyfile(history_template, path)
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{path}")
    with closing(sqlite3.connect(path)) as connection, connection:
        conversation, person = connection.execute(
            "SELECT c.id,c.person_id FROM canonical_conversations c "
            "JOIN chat_events e ON e.canonical_conversation_id=c.id WHERE e.id=2"
        ).fetchone()
        for name, event_id in (("known", 2), ("unknown", None)):
            connection.execute(
                "INSERT INTO social_operation_receipts(id,source_turn_id,tool_call_id,"
                "source_conversation_id,action,payload_hash,target_kind,target_id,status,"
                "platform_reference,created_at,updated_at,event_id) "
                "VALUES(?,?,?,?,'send_message',?,'person',?,'succeeded','native',"
                "'2026-10-22','2026-10-22',?)",
                (name, name, name, conversation, "a" * 64, person, event_id),
            )
    command.upgrade(Config("alembic.ini"), "head")
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("PRAGMA foreign_keys=ON")
        assert connection.execute(
            "SELECT original_event_id,original_conversation_id FROM social_operation_receipts "
            "WHERE id='known'"
        ).fetchone() == (2, conversation)
        assert connection.execute(
            "SELECT original_event_id,original_conversation_id FROM social_operation_receipts "
            "WHERE id='unknown'"
        ).fetchone() == (None, None)
        connection.execute("DELETE FROM chat_events WHERE id=2")
        assert connection.execute(
            "SELECT event_id,original_event_id,original_conversation_id,status "
            "FROM social_operation_receipts WHERE id='known'"
        ).fetchone() == (None, 2, conversation, "succeeded")
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()
