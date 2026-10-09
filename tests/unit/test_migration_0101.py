"""0101 stores one canonical outcome per legacy receipt; the original bytes stay."""

import asyncio
import importlib
import json
import sqlite3
import time

from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from tests.support.social_identity_cases import social_env

from qq_ai_bot.runtime.work_repository import WorkRepository

MIGRATION = "migrations.versions.0101_canonical_effect_outcome"


def _run(connection, migration, monkeypatch):
    monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
    migration.upgrade()


async def _seed(database, tmp_path):
    env = await social_env(database, tmp_path)
    repository = WorkRepository(database)
    lease = await repository.acquire(env.context.conversation_id, 1)
    item = await repository.accept(lease, source_key="legacy", source={}, goal="g")
    rows = {
        "ok": {"result": json.dumps({"ok": True, "data": {"status": "succeeded"}})},
        "pending": {"result": json.dumps({"ok": True, "data": {"run_id": "r", "pending": True}})},
        "truncated": {"result": json.dumps({"ok": True, "truncated": True})},
        "garbage": {"result": "not json"},
        "typed": {"outcome": {"ok": True, "side_effecting": False}, "result": "{}"},
        "final": {"transport_accepted": True, "message_id": "1"},
    }
    async with database.sessions() as session, session.begin():
        for key, receipt in rows.items():
            await session.execute(
                text(
                    "INSERT INTO runtime_work_effects "
                    "(effect_key, work_id, kind, state, receipt_json, created, updated) "
                    "VALUES (:k, :w, :kind, 'accepted', :r, :t, :t)"
                ),
                {
                    "k": key,
                    "w": item["id"],
                    "kind": "final" if key == "final" else "tool",
                    "r": json.dumps(receipt),
                    "t": time.time(),
                },
            )
    return repository, lease, item


async def _receipts(database):
    async with database.sessions() as session:
        rows = await session.execute(
            text("SELECT effect_key, receipt_json FROM runtime_work_effects")
        )
        return {key: json.loads(raw) for key, raw in rows}


async def test_legacy_results_get_canonical_outcome_and_sql_reads_only_it(
    database, tmp_path, monkeypatch
):
    migration = importlib.import_module(MIGRATION)
    assert (migration.revision, migration.down_revision) == ("0101", "0100")
    repository, lease, item = await _seed(database, tmp_path)
    before = await _receipts(database)
    async with database.engine.begin() as connection:
        await connection.run_sync(_run, migration, monkeypatch)
    after = await _receipts(database)
    for key in ("ok", "pending", "truncated", "garbage"):
        # The original result is untouched; only an outcome was added.
        assert after[key]["result"] == before[key]["result"]
        assert after[key]["outcome"]["legacy_migrated"] is True
    assert after["ok"]["outcome"]["ok"] is True and not after["ok"]["outcome"]["uncertain"]
    assert after["pending"]["outcome"]["pending"] is True
    assert after["truncated"]["outcome"]["uncertain"] is True
    assert after["garbage"]["outcome"]["uncertain"] is True
    assert after["typed"] == before["typed"] and after["final"] == before["final"]
    # SQL fences now come only from canonical outcome.* fields.
    unresolved = {
        row["effect_key"]
        for row in await repository.effect_evidence(lease, item["id"], only_unresolved=True)
    }
    assert unresolved == {"pending", "truncated", "garbage"}
    # Re-running is a no-op.
    async with database.engine.begin() as connection:
        await connection.run_sync(_run, migration, monkeypatch)
    assert await _receipts(database) == after


async def test_late_legacy_rewrite_of_migrated_receipt_is_idempotent(
    database, tmp_path, monkeypatch
):
    migration = importlib.import_module(MIGRATION)
    repository, _lease, _item = await _seed(database, tmp_path)
    async with database.engine.begin() as connection:
        await connection.run_sync(_run, migration, monkeypatch)
    migrated = (await _receipts(database))["ok"]
    # The same original receipt, rewritten late without the migrated outcome.
    await repository.record_effect("ok", "accepted", {"result": migrated["result"]})
    assert (await _receipts(database))["ok"] == migrated


async def test_fresh_upgrade_reaches_0101_head(tmp_path, monkeypatch):
    path = tmp_path / "head.sqlite3"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{path.as_posix()}")
    config = Config("alembic.ini")
    await asyncio.to_thread(command.upgrade, config, "head")
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == ("0101",)
    await asyncio.to_thread(command.downgrade, config, "0100")
