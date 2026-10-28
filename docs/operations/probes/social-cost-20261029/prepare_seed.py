"""Prepare a current fixture seed from the same known 0154 benchmark fixture."""

import argparse
import hashlib
import json
import shutil
import sqlite3
import time
from pathlib import Path

from alembic import command
from alembic.config import Config


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("before", type=Path)
    parser.add_argument("after", type=Path)
    args = parser.parse_args()
    started = time.perf_counter()
    shutil.copyfile(args.before, args.after)
    with sqlite3.connect(args.after) as connection:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS alembic_version"
            "(version_num VARCHAR(32) PRIMARY KEY NOT NULL)"
        )
        connection.execute("DELETE FROM alembic_version")
        connection.execute("INSERT INTO alembic_version VALUES('0154')")
    command.upgrade(Config("alembic.ini"), "head")
    elapsed = time.perf_counter() - started
    rows = {}
    renamed = {
        "chat_events": {"platform_message_id": "origin_key"},
        "canonical_event_receipts": {"platform_message_id": "ingress_key"},
    }
    with sqlite3.connect(args.before) as before, sqlite3.connect(args.after) as after:
        tables = [
            row[0]
            for row in before.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' AND name!='alembic_version'"
            )
        ]
        for table in tables:
            columns = [row[1] for row in before.execute(f'PRAGMA table_info("{table}")')]
            projection = ",".join(
                '"' + renamed.get(table, {}).get(column, column) + '"' for column in columns
            )
            original = before.execute(f'SELECT * FROM "{table}"').fetchall()
            assert original == after.execute(f'SELECT {projection} FROM "{table}"').fetchall(), (
                table
            )
            rows[table] = len(original)
    with args.before.open("rb") as source, args.after.open("rb") as target:
        report = {
            "migration_seconds_excluded": elapsed,
            "original_table_rows_identical": True,
            "tables_checked": len(tables),
            "row_counts": rows,
            "sha256_before": hashlib.file_digest(source, "sha256").hexdigest(),
            "sha256_after": hashlib.file_digest(target, "sha256").hexdigest(),
        }
    (args.after.parent / "seed-comparison.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
