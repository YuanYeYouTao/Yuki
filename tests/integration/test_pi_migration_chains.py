"""Actual historical producers, complete Alembic chains and new producer guard."""

import asyncio
import io
import json
import os
import sqlite3
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from tests.support.codemode_cases import FakeDomain, build_host, requires_worker, run_code
from tests.support.work_session import WorkSession

from qq_ai_bot.domain.messages import ChatMessage
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.runtime.work_control import WorkControl
from qq_ai_bot.runtime.work_repository import WorkRepository
from qq_ai_bot.services.turn_transcript import TurnTranscript

ROOT = Path(__file__).resolve().parents[2]
# The unified head follows main 0092–0095. Invocation facts move to 0096;
# historical upgrade, receipt preservation and old-producer refusal assertions
# remain unchanged apart from the expected target revision.
BASELINES = (
    ("755f7250e0ac465e57e748ea2e6583d1a76353b0", "0081"),
    ("8204b28ebc8939213dae60dbab94ab1c16d1263a", "0091"),
)
TABLES = (
    "memory_facts",
    "tool_artifacts",
    "runtime_work",
    "runtime_work_journal",
    "runtime_work_budgets",
    "runtime_work_effects",
    "social_operation_receipts",
    "sandbox_task_runs",
    "sandbox_task_continuations",
    "plugin_installations",
    "automations",
    "automation_runs",
    "runtime_automation_cursors",
)


def historical_database(tmp_path, sha):
    source = tmp_path / "historical-source"
    source.mkdir()
    archive = subprocess.run(
        ["git", "archive", sha, "src", "migrations", "alembic.ini", "config/persona.md"],
        cwd=ROOT,
        capture_output=True,
        check=True,
    ).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as files:
        files.extractall(source, filter="data")
    path = tmp_path / "legacy.sqlite3"
    url = f"sqlite+aiosqlite:///{path}"
    environment = {**os.environ, "DATABASE_URL": url, "PYTHONPATH": str(source / "src")}
    for args in (
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        [
            sys.executable,
            str(ROOT / "tests/support/pi_migration_seed.py"),
            url,
            str(tmp_path / "identities.json"),
        ],
    ):
        result = subprocess.run(args, cwd=source, env=environment, capture_output=True, text=True)
        assert result.returncode == 0, result.stderr[-6000:]
    return path, json.loads((tmp_path / "identities.json").read_text())


def facts(path, columns=None):
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        if columns is None:
            columns = {
                table: [row[1] for row in db.execute(f'PRAGMA table_info("{table}")')]
                for table in TABLES
            }
            # 0086 adds verified result metadata; it may enrich the original
            # receipt but must preserve operation identity and terminal state.
            columns["runtime_work_effects"].remove("receipt_json")
        rows = {}
        for table, names in columns.items():
            assert names, f"fixture table absent: {table}"
            select = ",".join(f'"{name}"' for name in names)
            rows[table] = sorted(db.execute(f'SELECT {select} FROM "{table}"').fetchall())
            assert rows[table], f"fixture has no lawful records: {table}"
        return columns, rows


@pytest.mark.parametrize("sha,head", BASELINES)
async def test_complete_legacy_chain_preserves_all_original_domain_records(
    tmp_path, monkeypatch, sha, head
):
    path, identities = await asyncio.to_thread(historical_database, tmp_path, sha)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT version_num FROM alembic_version").fetchone()[0] == head
    columns, before = facts(path)
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{path}")
    await asyncio.to_thread(command.upgrade, Config(str(ROOT / "alembic.ini")), "head")
    assert facts(path, columns)[1] == before
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT version_num FROM alembic_version").fetchone()[0] == "0101"
        assert db.execute("SELECT model_limit,tool_limit FROM runtime_work_budgets").fetchone() == (
            7,
            9,
        )
        for (raw,) in db.execute("SELECT receipt_json FROM runtime_work_effects"):
            assert "invocation" not in json.loads(raw) and "composition" not in json.loads(raw)
        assert (
            db.execute(
                "SELECT status FROM social_operation_receipts WHERE id=?",
                (identities["social_id"],),
            ).fetchone()[0]
            == "uncertain"
        )


@requires_worker
async def test_new_children_after_historical_upgrade_block_real_downgrade(tmp_path, monkeypatch):
    path, original = await asyncio.to_thread(historical_database, tmp_path, BASELINES[1][0])
    url = f"sqlite+aiosqlite:///{path}"
    monkeypatch.setenv("DATABASE_URL", url)
    config = Config(str(ROOT / "alembic.ini"))
    await asyncio.to_thread(command.upgrade, config, "head")
    database = Database(url)
    try:
        repo = WorkRepository(database)
        lease = await repo.acquire(original["conversation_id"], 1)

        async def validate():
            assert await repo.valid(lease)

        current = await repo.get(original["work_id"])
        control = WorkControl(
            repo, lease, current["source_key"], json.loads(current["source_json"]), validate
        )
        control.current = current
        owner = WorkSession(control, "current-contract")
        control.session = owner
        await owner.restore(TurnTranscript((ChatMessage("user", "continue same task"),)))
        domain = FakeDomain()
        env = build_host(owner, domain)
        result, _outer = await run_code(env, "await yuki_lookup({'legacy': True})")
        assert result["status"] == "completed"
        assert domain.log == [("lookup", {"legacy": True})]
        with sqlite3.connect(path) as db:
            before = db.execute(
                "SELECT effect_key,state,receipt_json FROM runtime_work_effects ORDER BY effect_key"
            ).fetchall()
        historical = tmp_path / "historical-source"
        old_producer = await asyncio.to_thread(
            subprocess.run,
            [sys.executable, "-m", "qq_ai_bot.cli", "init-db"],
            cwd=historical,
            env={**os.environ, "DATABASE_URL": url, "PYTHONPATH": str(historical / "src")},
            text=True,
            capture_output=True,
        )
        assert old_producer.returncode != 0
        assert "0101" in old_producer.stderr and "locate revision" in old_producer.stderr
        with pytest.raises(RuntimeError, match="Speech retirement cannot restore deleted facts"):
            await asyncio.to_thread(command.downgrade, config, "0091")
        with sqlite3.connect(path) as db:
            assert (
                db.execute(
                    "SELECT effect_key,state,receipt_json FROM runtime_work_effects "
                    "ORDER BY effect_key"
                ).fetchall()
                == before
            )
            assert db.execute("SELECT version_num FROM alembic_version").fetchone()[0] == "0101"
    finally:
        await database.close()
