"""Verify a stopped Bot's database and associated private evidence snapshot."""

from __future__ import annotations

import argparse
import hashlib
import sqlite3
from pathlib import Path


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            result.update(block)
    return result.hexdigest()


def verify(database_path: Path, data_directory: Path) -> dict[str, int]:
    counts = {"protocol_objects": 0, "tool_artifacts": 0}
    with sqlite3.connect(f"{database_path.resolve().as_uri()}?mode=ro", uri=True) as database:
        if database.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("backup database integrity failure")
        if database.execute("PRAGMA foreign_key_check").fetchone():
            raise RuntimeError("backup foreign-key failure")
        tables = {
            row[0] for row in database.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if "runtime_protocol_refs" in tables:
            invalid = database.execute(
                "SELECT 1 FROM runtime_protocol_refs r LEFT JOIN runtime_protocol_objects o "
                "ON r.sha256=o.sha256 WHERE o.sha256 IS NULL OR o.deleting=1 LIMIT 1"
            ).fetchone()
            if invalid is not None:
                raise RuntimeError("backup contains missing/deleting owned protocol metadata")
            for key, size in database.execute(
                "SELECT DISTINCT o.sha256,o.byte_size FROM runtime_protocol_objects o "
                "JOIN runtime_protocol_refs r ON r.sha256=o.sha256"
            ):
                path = data_directory / "work-protocol" / key[:2] / key[2:]
                if not path.is_file() or path.stat().st_size != size or digest(path) != key:
                    raise RuntimeError(f"backup protocol object incomplete: {key}")
                counts["protocol_objects"] += 1
        if "tool_artifacts" in tables:
            columns = {row[1] for row in database.execute("PRAGMA table_info(tool_artifacts)")}
            if "sha256" in columns:
                for relative, size, expected in database.execute(
                    "SELECT relative_path,byte_size,sha256 FROM tool_artifacts WHERE deleting=0"
                ):
                    root = (data_directory / "tool_artifacts").resolve()
                    path = (root / relative).resolve()
                    if not path.is_relative_to(root):
                        raise RuntimeError("backup artifact path escapes evidence directory")
                    if not path.is_file() or path.stat().st_size != size:
                        raise RuntimeError("backup tool artifact incomplete")
                    if expected is not None and digest(path) != expected:
                        raise RuntimeError("backup tool artifact corrupt")
                    counts["tool_artifacts"] += 1
    return counts


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path)
    parser.add_argument("data_directory", type=Path)
    arguments = parser.parse_args()
    print(verify(arguments.database, arguments.data_directory))
