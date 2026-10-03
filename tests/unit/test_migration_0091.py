"""Admission migration owns one identity-only table and its event lifecycle."""

import asyncio
import sqlite3

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text


async def test_frozen_admission_migration_matches_metadata_and_preserves_old_tables(
    database, tmp_path, monkeypatch
):
    path = tmp_path / "ordinary-migration.sqlite3"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{path.as_posix()}")
    config = Config("alembic.ini")
    await asyncio.to_thread(command.upgrade, config, "0090")
    with sqlite3.connect(path) as connection:
        old_tables = {
            r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    await asyncio.to_thread(command.upgrade, config, "0091")
    async with database.engine.connect() as connection:
        metadata_columns = (
            await connection.execute(text("PRAGMA table_info(ordinary_turn_admissions)"))
        ).all()
        metadata_fks = (
            await connection.execute(text("PRAGMA foreign_key_list(ordinary_turn_admissions)"))
        ).all()
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA table_info(ordinary_turn_admissions)").fetchall() == [
            tuple(r) for r in metadata_columns
        ]
        assert set(
            connection.execute("PRAGMA foreign_key_list(ordinary_turn_admissions)").fetchall()
        ) == {tuple(r) for r in metadata_fks}
        new_tables = {
            r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert new_tables - old_tables == {"ordinary_turn_admissions"}
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    await asyncio.to_thread(command.downgrade, config, "0090")
    with sqlite3.connect(path) as connection:
        assert {
            r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        } == old_tables
    await asyncio.to_thread(command.upgrade, config, "0091")


async def test_admitted_fact_blocks_real_downgrade_without_changing_head_or_facts(
    tmp_path, monkeypatch
):
    from tests.conftest import MemorySender
    from tests.unit.test_ordinary_admission import harness_for, message

    from qq_ai_bot.identity.canonical_repository import ensure_person, ensure_presence, ensure_space
    from qq_ai_bot.persistence.database import Database

    path = tmp_path / "retained-admission.sqlite3"
    url = f"sqlite+aiosqlite:///{path.as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    config = Config("alembic.ini")
    await asyncio.to_thread(command.upgrade, config, "0091")
    database = Database(url)
    try:
        harness = harness_for(database)
        async with database.immediate_session() as session:
            await ensure_person(session, "1001")
            await ensure_presence(session, "8000")
            await ensure_space(session, "2001", require_mention=True)
        assert (
            await harness.processor.handle(message(direct=True), MemorySender())
        ).reason == "chat"
        with sqlite3.connect(path) as connection:
            before = {
                table: connection.execute(f"SELECT * FROM {table}").fetchall()
                for table in ("ordinary_turn_admissions", "chat_events", "alembic_version")
            }
        assert len(before["ordinary_turn_admissions"]) == 1
        with pytest.raises(RuntimeError, match="ordinary admissions exist"):
            await asyncio.to_thread(command.downgrade, config, "0090")
        with sqlite3.connect(path) as connection:
            assert {
                table: connection.execute(f"SELECT * FROM {table}").fetchall() for table in before
            } == before
            assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        await database.close()
