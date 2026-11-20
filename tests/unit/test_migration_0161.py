"""0161 adds the two nullable observation columns to model_invocations (#649).

Failure cases written before the migration: existing rows gain invented
values; the upgrade rewrites or loses rows; the schema has more than one head
or the ORM disagrees with it; downgrade cannot drop the columns.
"""

import sqlite3
from contextlib import closing

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

from yuki.model_runtime.db_models import ModelInvocationModel

OBSERVATION = {"request_kind", "request_chain_id"}


def _columns(path):
    with closing(sqlite3.connect(path)) as connection:
        return {row[1]: row for row in connection.execute("PRAGMA table_info(model_invocations)")}


def test_upgrade_keeps_rows_and_leaves_history_unknown(tmp_path, monkeypatch):
    path = tmp_path / "invocations.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{path}")
    config = Config("alembic.ini")
    command.upgrade(config, "0159")
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            "INSERT INTO model_invocations(task,profile_id,provider,model,success,"
            "prompt_tokens,cached_prompt_tokens,latency_seconds,created_at) "
            "VALUES('chat_agent','p','openai','m',1,100,80,0.5,'2026-11-18 04:44:08')"
        )
        before = connection.execute("SELECT rowid, * FROM model_invocations").fetchall()
    assert not OBSERVATION & set(_columns(path))

    command.upgrade(config, "0161")
    columns = _columns(path)
    assert OBSERVATION <= set(columns)
    for name in OBSERVATION:
        # cid, name, type, notnull, default, pk: nullable, no default.
        assert columns[name][3] == 0 and columns[name][4] is None, columns[name]
    with closing(sqlite3.connect(path)) as connection:
        after = connection.execute("SELECT rowid, * FROM model_invocations").fetchall()
        assert [row[: len(before[0])] for row in after] == before
        assert connection.execute(
            "SELECT request_kind, request_chain_id FROM model_invocations"
        ).fetchall() == [(None, None)]
    orm = {column.name for column in ModelInvocationModel.__table__.columns}
    assert orm == set(columns)

    command.downgrade(config, "0159")
    assert not OBSERVATION & set(_columns(path))
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("SELECT rowid, * FROM model_invocations").fetchall() == before


def test_bundled_migrations_have_one_head():
    heads = ScriptDirectory.from_config(Config("alembic.ini")).get_heads()
    assert len(heads) == 1, heads
