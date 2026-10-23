"""History source seeks use migrated indexes without changing original facts."""

import shutil
import sqlite3
from collections import Counter
from contextlib import closing

import pytest
from alembic import command
from alembic.config import Config
from tests.unit.test_migration_0119 import _seed_0118

INDEXES = {
    "ix_conversations_space": "space_id",
    "ix_conversations_identity_binding": "identity_binding_id",
    "ix_conversations_space_binding": "space_binding_id",
}


@pytest.fixture(scope="module")
def previous_schema(tmp_path_factory):
    path = tmp_path_factory.mktemp("history-source-upgrade") / "0147.db"
    with pytest.MonkeyPatch.context() as patch:
        _seed_0118(path, patch)
        command.upgrade(Config("alembic.ini"), "0147")
    return path


def test_source_index_upgrade_preserves_all_original_rows_and_schema(
    previous_schema, tmp_path, monkeypatch
):
    path = tmp_path / "scope-indexes.db"
    shutil.copyfile(previous_schema, path)
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{path.as_posix()}")
    with closing(sqlite3.connect(path)) as connection:
        schema = connection.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name"
        ).fetchall()
        facts = {
            name: Counter(connection.execute(f'SELECT * FROM "{name}"').fetchall())
            for kind, name, _table, _sql in schema
            if kind == "table" and name != "alembic_version"
        }
    command.upgrade(Config("alembic.ini"), "0148")
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == ("0148",)
        after = connection.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name"
        ).fetchall()
        assert [row for row in after if row[1] not in INDEXES] == schema
        assert {row[1] for row in after} - {row[1] for row in schema} == INDEXES.keys()
        for table, rows in facts.items():
            assert Counter(connection.execute(f'SELECT * FROM "{table}"').fetchall()) == rows
        for name, column in INDEXES.items():
            assert [row[2] for row in connection.execute(f"PRAGMA index_info({name})")] == [
                column,
                "id",
            ]
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()
