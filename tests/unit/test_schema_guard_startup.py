"""Startup checks metadata; full reference audits belong to offline upgrades."""

import asyncio
import shutil
import sqlite3

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import event, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from yuki.conversation.projection_chunks_schema import PROJECTION_TRIGGERS_CURRENT
from yuki.persistence.database import Database
from yuki.persistence.schema_guard import CanonicalSchemaError, require_canonical_schema


@pytest.fixture(scope="module")
def schema_template(tmp_path_factory):
    path = tmp_path_factory.mktemp("startup-schema") / "template.db"
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{path}")
        command.upgrade(Config("alembic.ini"), "head")
    return path


@pytest.fixture
def schema_path(schema_template, tmp_path):
    path = tmp_path / "startup.db"
    shutil.copyfile(schema_template, path)
    return path


def test_startup_does_not_scan_business_foreign_keys(schema_path):
    # An explicit offline audit can find a damaged old reference. Startup's
    # metadata contract does not repeat that scan on every process restart.
    with sqlite3.connect(schema_path) as connection:
        connection.execute(
            "INSERT INTO memberships(canonical_person_id,canonical_space_id,"
            "first_seen_at,last_seen_at) VALUES('missing-person','missing-space',"
            "'2026-10-19','2026-10-19')"
        )
        assert len(connection.execute("PRAGMA foreign_key_check").fetchall()) == 2
    statements = []

    def capture(_connection, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement.lower())

    event.listen(Engine, "before_cursor_execute", capture)
    try:
        asyncio.run(require_canonical_schema(f"sqlite+aiosqlite:///{schema_path}"))
    finally:
        event.remove(Engine, "before_cursor_execute", capture)
    assert statements
    assert not any(
        marker in statement
        for statement in statements
        for marker in ("foreign_key_check", "integrity_check", "quick_check")
    )
    assert all(
        "select" in statement or statement.lstrip().startswith("pragma") for statement in statements
    )


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ("UPDATE alembic_version SET version_num='0140'", "migration head is unsupported"),
        ("DROP TABLE accounts", "canonical schema is incomplete"),
        (
            "DROP INDEX ix_chat_events_social_source_author",
            "social source index is missing or changed",
        ),
        (
            "DROP TRIGGER " + next(iter(PROJECTION_TRIGGERS_CURRENT)),
            "projection invalidation trigger is missing or changed",
        ),
    ],
)
def test_startup_still_rejects_changed_version_and_metadata(schema_path, change, message):
    with sqlite3.connect(schema_path) as connection:
        connection.execute(change)
    with pytest.raises(CanonicalSchemaError, match=message):
        asyncio.run(require_canonical_schema(f"sqlite+aiosqlite:///{schema_path}"))


async def test_runtime_connections_still_enforce_foreign_keys(schema_path):
    database = Database(f"sqlite+aiosqlite:///{schema_path}")
    try:
        async with database.sessions() as session:
            assert await session.scalar(text("PRAGMA foreign_keys")) == 1
            with pytest.raises(IntegrityError, match="FOREIGN KEY constraint failed"):
                await session.execute(
                    text(
                        "INSERT INTO memberships(canonical_person_id,canonical_space_id,"
                        "first_seen_at,last_seen_at) VALUES('missing-person','missing-space',"
                        "'2026-10-19','2026-10-19')"
                    )
                )
    finally:
        await database.close()


def test_offline_0141_upgrade_checks_original_foreign_keys_before_commit(tmp_path, monkeypatch):
    path = tmp_path / "damaged-old.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{path}")
    config = Config("alembic.ini")
    command.upgrade(config, "0140")
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO memberships(canonical_person_id,canonical_space_id,group_card,"
            "first_seen_at,last_seen_at) VALUES('missing-person','missing-space','',"
            "'2026-10-19','2026-10-19')"
        )
    with pytest.raises(ValueError, match="0141 original references are inconsistent"):
        command.upgrade(config, "0141")
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchall() == [
            ("0140",)
        ]
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='identity_bindings'"
        ).fetchone() == ("identity_bindings",)
        assert not connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='account_assignments'"
        ).fetchall()
