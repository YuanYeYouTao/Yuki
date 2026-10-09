"""MIG-01: actual Pi stop schemas, then current offline upgrades without replay."""

import hashlib
import io
import json
import os
import sqlite3
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest
from tests.unit.test_migration_0049 import _restore_historical_0048, _schema_shape

ROOT = Path(__file__).resolve().parents[2]
PI = "e7bc7d3275b09ddc5363bbb1eb9d2ee255f276ed"
WORK = "11111111-1111-4111-8111-111111111111"
STOP_SCHEMA_SHA256 = {
    "0058": "e943b14e1aaaa6b1363fcf2956385be6c81a780d5b6b9e8dbcb4d03a560ee0dd",
    "0060": "0c84098fa5e9b7fb5e4e44029b1351552aefeac5f92ba4eff6c91f8bbb5681ae",
    "0088": "2010bd5be6f324a50c5a582ec4b731f86a2eb2cf5d33af2760bc8ddc000873f2",
}


@pytest.fixture(scope="module")
def frozen_pi(tmp_path_factory):
    source = tmp_path_factory.mktemp("frozen-pi")
    archive = subprocess.check_output(
        ["git", "archive", PI, "src", "migrations", "alembic.ini", "config/persona.md"],
        cwd=ROOT,
    )
    with tarfile.open(fileobj=io.BytesIO(archive)) as files:
        files.extractall(source, filter="data")
    return source


def upgrade(source, path, revision):
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", revision],
        cwd=source,
        env={
            **os.environ,
            "DATABASE_URL": "sqlite+aiosqlite:///" + path.as_posix(),
            "PYTHONPATH": str(source / "src"),
        },
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr[-8000:]


def seed(path, folder):
    """SQL uses the asserted historical schema, never present-day ORM defaults."""
    attachment = folder / "retained-artifact.txt"
    attachment.write_bytes(b"original attachment\x00\xff")
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA foreign_keys=ON")
        conversation = db.execute("SELECT id FROM canonical_conversations LIMIT 1").fetchone()[0]
        source = json.dumps({"origin": "chat", "conversation_id": conversation, "event_id": 1})
        db.execute(
            "INSERT INTO runtime_work(id,conversation_id,generation,source_key,source_json,"
            "goal,state,model_requests,tool_calls,sent_messages,created,updated) "
            "VALUES(?,?,1,'stop-point-source',?,'Retain original task','suspended',3,5,2,1,1)",
            (WORK, conversation, source),
        )
        db.execute(
            "INSERT INTO runtime_work_budgets(root_id,models,tools,model_limit,tool_limit) "
            "VALUES(?,3,5,7,9)",
            (WORK,),
        )
        for state in ("accepted", "unknown"):
            receipt = json.dumps(
                {
                    "original_execution_id": "original-" + state,
                    "result": json.dumps(
                        {
                            "ok": state == "accepted",
                            "data": {
                                "run_id": "original-" + state,
                                "uncertain": state == "unknown",
                            },
                        }
                    ),
                }
            )
            db.execute(
                "INSERT INTO runtime_work_effects VALUES(?,?,'tool',?,?,1,1)",
                ("original-operation-" + state, WORK, state, receipt),
            )
        db.execute(
            "INSERT INTO runtime_work_journal VALUES(?,?,'original-contract',1,'waiting',?,1)",
            (WORK, "original-chain", '{"original_execution_id":"original-unknown"}'),
        )
        db.execute(
            "INSERT INTO tool_artifacts(handle_id,provider_id,tool_name,relative_path,media_type,"
            "byte_size,created_at,expires_at) VALUES('retained','core','lookup',?,'text/plain',?,"
            "'2026-01-01','2030-01-01')",
            (attachment.name, attachment.stat().st_size),
        )
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    return attachment


def retained(path):
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert db.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        rows = {
            table: db.execute(sql).fetchall()
            for table, sql in {
                "work": "SELECT id,conversation_id,source_key,source_json,state,model_requests,"
                "tool_calls,sent_messages FROM runtime_work",
                "budget": "SELECT * FROM runtime_work_budgets",
                "journal": "SELECT * FROM runtime_work_journal",
                "artifact": "SELECT handle_id,provider_id,tool_name,relative_path,media_type,"
                "byte_size,created_at,expires_at FROM tool_artifacts",
            }.items()
        }
        rows["effects"] = [
            (key, owner, state, {k: v for k, v in json.loads(raw).items() if k != "outcome"})
            for key, owner, state, raw in db.execute(
                "SELECT effect_key,work_id,state,receipt_json FROM runtime_work_effects "
                "ORDER BY effect_key"
            )
        ]
        return rows


@pytest.mark.parametrize("stop", ["0058", "0060", "0088"])
def test_original_stop_schema_and_facts_survive_head(tmp_path, frozen_pi, stop):
    historical, current = tmp_path / "historical.sqlite3", tmp_path / "current.sqlite3"
    _restore_historical_0048(historical)
    upgrade(frozen_pi, historical, stop)
    upgrade(ROOT, current, stop)
    original_shape = _schema_shape(historical)
    assert _schema_shape(current) == original_shape
    fingerprint = hashlib.sha256(json.dumps(original_shape, sort_keys=True).encode()).hexdigest()
    assert fingerprint == STOP_SCHEMA_SHA256[stop]
    print(f"MIG-01 stop={stop} original_schema_sha256={fingerprint}")
    attachment = seed(historical, tmp_path)
    original_file = attachment.read_bytes()
    before = retained(historical)
    upgrade(ROOT, historical, "head")
    assert retained(historical) == before
    assert attachment.read_bytes() == original_file
    with sqlite3.connect(historical) as db:
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == ("0101",)
