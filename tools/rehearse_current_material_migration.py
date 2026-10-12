"""Run the real 0109/0110/0111 migrations on a read-only snapshot's explicit copy.

Uses installed Python/Alembic only. No application lifecycle, model, protocol
decryption, Worker or network is started. Reports counts and hashes, never body.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import time
from importlib import import_module, metadata
from pathlib import Path
from typing import Any

PRIVATE_TABLES = (
    "runtime_work",
    "runtime_work_journal",
    "runtime_work_effects",
    "runtime_work_inputs",
    "runtime_work_budgets",
    "runtime_automation_budgets",
    "runtime_automation_cursors",
    "runtime_protocol_objects",
    "runtime_protocol_refs",
    "runtime_protocol_usage",
    "automation_runs",
    "automation_step_runs",
    "memory_facts",
    "memory_evidence",
    "memory_mutation_receipts",
    "memory_tool_receipts",
)
OLD_HEADER = "本轮运行资料（按 trust 字段区分可信度）："


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def file_digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as reader:
        for chunk in iter(lambda: reader.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def rows_digest(connection, table, *, predicate="", values=()) -> dict[str, Any]:
    result = hashlib.sha256()
    cursor = connection.execute(f'SELECT * FROM "{table}" {predicate} ORDER BY rowid', values)
    columns = [column[0] for column in cursor.description]
    count = 0
    for row in cursor:
        encoded = [value.hex() if isinstance(value, bytes) else value for value in row]
        result.update(json.dumps(encoded, ensure_ascii=False, separators=(",", ":")).encode())
        result.update(b"\n")
        count += 1
    return {"rows": count, "columns": columns, "sha256": result.hexdigest()}


def inventory(path: Path, original_version_max: int) -> dict[str, Any]:
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as reader:
        tables = {
            row[0] for row in reader.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        private = {table: rows_digest(reader, table) for table in PRIVATE_TABLES if table in tables}
        projections = list(reader.execute("SELECT payload_json FROM prompt_projections"))
        definitions = list(reader.execute("SELECT status,script_json FROM automations"))
        legacy = [
            (status, raw)
            for status, raw in definitions
            if not {"goal", "trigger"} <= json.loads(raw).keys()
        ]
        return {
            "schema": reader.execute("SELECT version_num FROM alembic_version").fetchone()[0],
            "latest_event_id": reader.execute("SELECT max(id) FROM chat_events").fetchone()[0],
            "private": private,
            "original_versions": rows_digest(
                reader,
                "automation_versions",
                predicate="WHERE id<=?",
                values=(original_version_max,),
            ),
            "context_notes": rows_digest(
                reader,
                "model_context_observations",
                predicate="WHERE source_key LIKE 'work-note:%'",
            ),
            "automatic_sources": reader.execute(
                "SELECT count(*) FROM model_context_observations "
                "WHERE source_key LIKE 'snapshot:%' "
                "AND json_extract(payload_json,'$.kind')='dynamic_snapshot'"
            ).fetchone()[0],
            "selections": reader.execute(
                "SELECT count(*) FROM model_context_selections"
            ).fetchone()[0],
            "compiled_material_selections": reader.execute(
                "SELECT count(*) FROM model_context_selections "
                "WHERE substr(json_extract(payload_json,'$.message.content'),1,?)=?",
                (len(OLD_HEADER), OLD_HEADER),
            ).fetchone()[0],
            "public_automatic_roots": reader.execute(
                "SELECT count(*) FROM model_context_selections s "
                "JOIN model_context_observations o "
                "ON o.id=json_extract(s.payload_json,'$.observation_id') "
                "WHERE o.source_key LIKE 'snapshot:%' "
                "AND json_extract(o.payload_json,'$.kind')='dynamic_snapshot'"
            ).fetchone()[0],
            "projection_count": len(projections),
            "nonempty_public_projections": sum(json.loads(row[0]) != [] for row in projections),
            "visible_material_column_present": any(
                row[1] == "visible_material_json"
                for row in reader.execute("PRAGMA table_info(prompt_projections)")
            ),
            "autonomy_source_claims_table_present": "autonomy_source_claims" in tables,
            "definition_count": len(definitions),
            "legacy_definition_count": len(legacy),
            "active_legacy_definition_count": sum(status == "active" for status, _ in legacy),
            "legacy_definition_statuses": {
                status: sum(value[0] == status for value in legacy) for status, _ in legacy
            },
            "version_count": reader.execute("SELECT count(*) FROM automation_versions").fetchone()[
                0
            ],
            "integrity": [row[0] for row in reader.execute("PRAGMA integrity_check")],
            "foreign_key_violations": [
                list(row) for row in reader.execute("PRAGMA foreign_key_check")
            ],
        }


def retirement_plan(path: Path) -> dict[str, Any]:
    import sqlalchemy as sa

    engine = sa.create_engine(
        "sqlite://", creator=lambda: sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    )
    try:
        with engine.connect() as reader:
            frozen = import_module("migrations.versions.0108_material_delta_projection")
            observations, affected, removable, _ = frozen._prepare_retirement(reader)
            publication = import_module("migrations.versions.0110_scheduled_main_tasks")
            definitions = [
                publication._task(dict(row))
                for row in reader.execute(sa.text("SELECT * FROM automations")).mappings()
            ]
        return {
            "observation_sources": len(observations),
            "affected_automatic_sources": len(affected),
            "eligible_unprotected_automatic_sources": len(removable),
            "protected_automatic_sources": len(affected) - len(removable),
            "affected_source_ids_sha256": digest(sorted(affected)),
            "eligible_source_ids_sha256": digest(sorted(removable)),
            "convertible_legacy_definitions": sum(isinstance(task, dict) for task in definitions),
            "already_main_definitions": sum(task is None for task in definitions),
            "archived_unrecoverable_definitions": sum(task is False for task in definitions),
        }
    finally:
        engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--copy", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    snapshot, target, root = (
        args.snapshot.resolve(),
        args.copy.resolve(),
        args.source_root.resolve(),
    )
    if target == snapshot:
        raise ValueError("The read-only snapshot and rehearsal copy must have distinct identities.")
    sys.path[:0] = [str(root / "src"), str(root)]
    os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{target.as_posix()}"
    target.parent.mkdir(parents=True, exist_ok=True)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    source_hash = file_digest(snapshot)
    print("Backing up the read-only snapshot to the explicit rehearsal copy.", flush=True)
    with sqlite3.connect(snapshot.as_uri() + "?mode=ro", uri=True) as reader:
        with sqlite3.connect(target) as writer:
            reader.backup(writer)
    with sqlite3.connect(target) as reader:
        original_version_max = reader.execute(
            "SELECT coalesce(max(id),0) FROM automation_versions"
        ).fetchone()[0]
    print("Hashing original rows and validating the frozen retirement plan.", flush=True)
    before = inventory(target, original_version_max)
    plan = retirement_plan(target)
    distribution = metadata.distribution("yuki-semantic-participation")
    direct = json.loads(distribution.read_text("direct_url.json") or "{}")
    report = {
        "source_snapshot": str(snapshot),
        "rehearsal_copy": str(target),
        "source_root": str(root),
        "source_size": snapshot.stat().st_size,
        "source_sha256": source_hash,
        "python": sys.version,
        "installed_participation_version": distribution.version,
        "installed_participation_commit": direct.get("vcs_info", {}).get("commit_id"),
        "scope": (
            "DDL rehearsal only, with installed dependencies; no full Main/SP compatibility claim."
        ),
        "before": before,
        "retirement_plan_before": plan,
        "migration_source_sha256": {
            name: file_digest(root / "migrations" / "versions" / name)
            for name in (
                "0109_current_activation_material.py",
                "0110_scheduled_main_tasks.py",
                "0111_memory_execution_sources.py",
            )
        },
        "stages": {},
    }
    from alembic import command
    from alembic.config import Config

    config = Config()
    config.set_main_option("script_location", str(root / "migrations"))
    for revision in ("0109", "0110", "0111"):
        print(f"Running actual Alembic upgrade to {revision} on the copy.", flush=True)
        command.upgrade(config, revision)
        current = inventory(target, original_version_max)
        report["stages"][revision] = {
            "inventory": current,
            "private_rows_unchanged": current["private"] == before["private"],
            "original_versions_unchanged": current["original_versions"]
            == before["original_versions"],
            "context_notes_unchanged": current["context_notes"] == before["context_notes"],
            "new_fk_violations": [
                row
                for row in current["foreign_key_violations"]
                if row not in before["foreign_key_violations"]
            ],
        }
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    report["retirement_plan_after"] = retirement_plan(target)
    report["source_snapshot_unchanged"] = file_digest(snapshot) == source_hash
    report["elapsed_seconds"] = time.monotonic() - started
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "report": str(args.report),
                "schema": "0111",
                "source_unchanged": report["source_snapshot_unchanged"],
                "private_rows_unchanged": report["stages"]["0111"]["private_rows_unchanged"],
            }
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
