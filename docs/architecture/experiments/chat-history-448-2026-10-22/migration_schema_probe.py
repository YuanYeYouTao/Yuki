"""Compare only SQLite schema metadata in two read-only databases.

Run on the database host, passing the existing 0143 database and upgraded isolated
snapshot. This reads no chat, account, configuration or credential rows.
"""

from __future__ import annotations

import collections
import hashlib
import json
import re
import sqlite3
import sys
from datetime import UTC, datetime
from urllib.parse import quote


def pragma(connection: sqlite3.Connection, kind: str, name: str) -> list[tuple]:
    escaped = name.replace('"', '""')
    return connection.execute(f'PRAGMA {kind}("{escaped}")').fetchall()


def normalized_default(value: str | None) -> str | None:
    if value == "('')":
        return "''"
    return value


def checks(sql: str) -> dict[str, str]:
    result = {}
    for match in re.finditer(r"CONSTRAINT\s+(\w+)\s+CHECK\s*\(", sql, re.I):
        start = position = match.end()
        level, quoted = 1, False
        while position < len(sql) and level:
            char = sql[position]
            if char == "'":
                if quoted and position + 1 < len(sql) and sql[position + 1] == "'":
                    position += 2
                    continue
                quoted = not quoted
            if not quoted:
                level += (char == "(") - (char == ")")
            position += 1
        result[match.group(1)] = re.sub(r"\s+", " ", sql[start : position - 1]).strip()
    return result


def snapshot(path: str) -> dict:
    with sqlite3.connect("file:" + quote(path, safe="/") + "?mode=ro", uri=True) as db:
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        objects = {
            row[1]: tuple(row)
            for row in db.execute("SELECT type,name,tbl_name,sql FROM sqlite_master")
        }
        tables = [name for name, row in objects.items() if row[0] == "table"]
        columns, foreign_keys, indices = {}, {}, {}
        for name in tables:
            columns[name] = [
                (*row[:4], normalized_default(row[4]), *row[5:])
                for row in pragma(db, "table_xinfo", name)
            ]
            groups = collections.defaultdict(list)
            for row in pragma(db, "foreign_key_list", name):
                groups[row[0]].append(tuple(row[1:]))
            foreign_keys[name] = sorted(tuple(sorted(group)) for group in groups.values())
        for name, row in objects.items():
            if row[0] == "index":
                indices[name] = pragma(db, "index_xinfo", name)
        version = db.execute("SELECT version_num FROM alembic_version").fetchone()[0]
        db.rollback()
    return {
        "version": version,
        "objects": objects,
        "columns": columns,
        "foreign_keys": foreign_keys,
        "indices": indices,
    }


def differences(first: dict, second: dict) -> dict:
    return {
        name: {"before": first[name], "after": second[name]}
        for name in sorted(first.keys() & second.keys())
        if first[name] != second[name]
    }


def main() -> None:
    before, after = map(snapshot, sys.argv[1:])
    original, upgraded = before["objects"], after["objects"]
    changed = differences(original, upgraded)
    common = original.keys() & upgraded.keys()
    original_chat_indices = sorted(
        name for name, row in original.items() if row[0] == "index" and row[2] == "chat_events"
    )
    warning_names = (
        "uq_memory_facts_active_canonical_self_key",
        "ix_runtime_work_query_scope_updated",
        "uq_canonical_conversations_space",
        "uq_canonical_conversations_binding",
    )
    result = {
        "generated_utc": datetime.now(UTC).isoformat(),
        "reader_sqlite": sqlite3.sqlite_version,
        "scope": "read-only schema metadata; no application rows or credentials",
        "versions": [before["version"], after["version"]],
        "object_counts": [len(original), len(upgraded)],
        "byte_identical_common_objects": len(common) - len(changed),
        "removed_objects": sorted(original.keys() - upgraded.keys()),
        "added_objects": sorted(upgraded.keys() - original.keys()),
        "changed_objects": {
            name: {
                "type": original[name][0],
                "before_sql_sha256": hashlib.sha256(original[name][3].encode()).hexdigest(),
                "after_sql_sha256": hashlib.sha256(upgraded[name][3].encode()).hexdigest(),
            }
            for name in changed
        },
        "column_differences": differences(before["columns"], after["columns"]),
        "foreign_key_differences": differences(before["foreign_keys"], after["foreign_keys"]),
        "index_xinfo_differences": differences(before["indices"], after["indices"]),
        "chat_named_checks": {
            "before": checks(original["chat_events"][3]),
            "after": checks(upgraded["chat_events"][3]),
        },
        "original_chat_indices": original_chat_indices,
        "original_chat_expression_indices": [
            name
            for name in original_chat_indices
            if any(row[1] == -2 for row in before["indices"][name])
        ],
        "warning_related_indices": {
            name: {
                "exists_before": name in original,
                "exists_after": name in upgraded,
                "byte_identical": original.get(name) == upgraded.get(name),
            }
            for name in warning_names
        },
        "chat_triggers": {
            label: sorted(
                name
                for name, row in objects.items()
                if row[0] == "trigger" and row[2] == "chat_events"
            )
            for label, objects in (("before", original), ("after", upgraded))
        },
    }
    print(json.dumps(result, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
