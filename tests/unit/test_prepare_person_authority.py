"""Legacy candidate export reads historical identity facts without assigning qualification."""

import json
import runpy
import sqlite3
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from tests.unit.test_migration_0119 import _seed_0118


def test_candidate_export_preserves_database_and_reports_historical_roots(tmp_path):
    database = tmp_path / "yuki.db"
    source, target = str(uuid4()), str(uuid4())
    with sqlite3.connect(database) as connection:
        connection.executescript(
            "CREATE TABLE persons (id TEXT PRIMARY KEY, display_name TEXT, enabled INTEGER, "
            "merged_into_person_id TEXT);"
            "CREATE TABLE identity_bindings (id TEXT PRIMARY KEY, person_id TEXT, platform TEXT, "
            "external_account_id TEXT, status TEXT, created_at TEXT);"
        )
        connection.executemany(
            "INSERT INTO persons VALUES (?, ?, ?, ?)",
            [(source, "old person", 0, target), (target, "current root", 1, None)],
        )
        connection.executemany(
            "INSERT INTO identity_bindings VALUES (?, ?, ?, ?, ?, ?)",
            [
                (str(uuid4()), source, "qq", "10001", "transferred", "1"),
                (str(uuid4()), target, "qq", "10001", "disabled", "2"),
                (str(uuid4()), target, "telegram", "10001", "active", "3"),
            ],
        )
    (tmp_path / ".env").write_text(
        "SUPERUSERS=10001,10002\nDATABASE_URL=sqlite+aiosqlite:///./yuki.db\n"
        "LLM_API_KEY=secret-must-not-be-exported\n",
        encoding="utf-8",
    )
    original = database.read_bytes()
    output = tmp_path / "candidates.json"
    script = Path(__file__).resolve().parents[2] / "scripts/prepare_person_authority.py"
    result = subprocess.run(
        [sys.executable, str(script), "--deployment-root", str(tmp_path), "--output", str(output)],
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(output.read_text("utf-8"))
    assert payload["qualification_granted"] is False
    historical, current = payload["candidates"][0]["bindings"]
    assert historical["status"] == "transferred"
    assert historical["person"]["id"] == source and historical["person"]["enabled"] == 0
    assert historical["current_root"]["id"] == target
    assert historical["merge_chain"] == [source, target]
    assert current["status"] == "disabled" and current["person"]["id"] == target
    assert payload["candidates"][1]["bindings"] == []
    assert database.read_bytes() == original
    assert "secret-must-not-be-exported" not in output.read_text("utf-8") + result.stdout


@pytest.mark.parametrize("revision", ["0119", "0128"])
def test_exporter_reads_actual_pre_person_merge_schema_without_migration(
    tmp_path, monkeypatch, revision
):
    database = tmp_path / "old-release.db"
    _seed_0118(database, monkeypatch)
    command.upgrade(Config("alembic.ini"), revision)
    with sqlite3.connect(database) as connection:
        assert "display_name" not in {
            row[1] for row in connection.execute("PRAGMA table_info(persons)")
        }
        owner = connection.execute(
            "SELECT person_id FROM identity_bindings WHERE external_account_id='0'"
        ).fetchone()[0]
    original = database.read_bytes()
    script = Path(__file__).resolve().parents[2] / "scripts/prepare_person_authority.py"
    exported = runpy.run_path(str(script))["export_candidates"](database, ("0", "unbound"))
    binding = exported["candidates"][0]["bindings"][0]
    assert binding["person"]["id"] == owner
    assert binding["person"]["display_name"] == ""
    assert binding["person"]["merged_into_person_id"] is None
    assert binding["current_root"]["id"] == owner and binding["merge_chain"] == [owner]
    assert exported["candidates"][1]["bindings"] == []
    assert exported["qualification_granted"] is False
    assert database.read_bytes() == original
