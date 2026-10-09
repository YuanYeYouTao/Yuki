"""Both actual 0092 experiment and 0095 main producers upgrade without restamping."""

import asyncio
import importlib
import json
import sqlite3

import pytest
from alembic import command
from alembic.config import Config
from tests.integration.test_pi_migration_chains import ROOT, facts, historical_database

from qq_ai_bot.persistence.schema_guard import require_canonical_schema


@pytest.mark.parametrize(
    "sha,head,upstream_indexes",
    [
        ("3d1d983828207049cb5e0806ad01b5008c841591", "0092", False),
        ("de9d0e3ae1d682ec6411df4d628db4ba061a5fd8", "0095", True),
        ("0f24a3b590d103eb547483161fb5873d5a78d032", "0096", True),
        ("25cd6083015924bf80405ca7c346f84a995f6ebb", "0096", True),
    ],
)
async def test_actual_branch_producer_upgrade_preserves_work_receipts_and_budgets(
    tmp_path, monkeypatch, sha, head, upstream_indexes
):
    path, _identities = await asyncio.to_thread(historical_database, tmp_path, sha)
    migration = importlib.import_module("migrations.versions.0096_invocation_effect_indexes")
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT version_num FROM alembic_version").fetchone()[0] == head
        for name, _table, _columns in migration.LEGACY_MAIN_INDEXES:
            assert (
                bool(db.execute("SELECT 1 FROM sqlite_master WHERE name=?", (name,)).fetchone())
                is upstream_indexes
            )
        if not upstream_indexes:
            # Preserve a versioned original effect as well as the real producer's
            # ordinary facts. The schema bridge must not rewrite its JSON/IDs.
            effect_key, raw = db.execute(
                "SELECT effect_key,receipt_json FROM runtime_work_effects LIMIT 1"
            ).fetchone()
            receipt = json.loads(raw)
            receipt["invocation"] = {
                "version": 1,
                "operation_id": effect_key,
                "parent_effect_key": None,
            }
            db.execute(
                "UPDATE runtime_work_effects SET receipt_json=? WHERE effect_key=?",
                (json.dumps(receipt), effect_key),
            )
            db.commit()
    columns, _ = facts(path)
    columns["runtime_work_effects"].append("receipt_json")
    before = facts(path, columns)[1]
    url = f"sqlite+aiosqlite:///{path}"
    monkeypatch.setenv("DATABASE_URL", url)
    config = Config(str(ROOT / "alembic.ini"))
    await asyncio.to_thread(command.upgrade, config, "head")
    await require_canonical_schema(url)
    assert facts(path, columns)[1] == before
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT version_num FROM alembic_version").fetchone()[0] == "0101"
        for name in (*migration.INDEXES, *(s[0] for s in migration.LEGACY_MAIN_INDEXES)):
            assert db.execute("SELECT 1 FROM sqlite_master WHERE name=?", (name,)).fetchone()
    # Re-running standard upgrade is idempotent, without manual stamp/DB replacement.
    await asyncio.to_thread(command.upgrade, config, "head")
    assert facts(path, columns)[1] == before
    # Speech facts have no lawful backward reconstruction, for every producer.
    # The failed downgrade rolls back earlier reverse steps in the same writer.
    with pytest.raises(RuntimeError, match="Speech retirement cannot restore deleted facts"):
        await asyncio.to_thread(command.downgrade, config, "0095")
    assert facts(path, columns)[1] == before
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT version_num FROM alembic_version").fetchone()[0] == "0101"
