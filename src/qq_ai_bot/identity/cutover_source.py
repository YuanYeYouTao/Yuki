"""Deterministic live source manifests for identity cutover.

Hashes every applied business table's row content, not row counts.
Alembic/SQLite internals and cutover bookkeeping are excluded.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Final

CUTOVER_BOOKKEEPING_TABLES: Final[frozenset[str]] = frozenset(
    {
        "identity_cutover_manifests",
        "identity_cutover_runs",
    }
)
ALEMBIC_INTERNAL_TABLES: Final[frozenset[str]] = frozenset({"alembic_version"})


def is_excluded_source_table(name: str) -> bool:
    if not name or name.startswith("sqlite_"):
        return True
    if name in ALEMBIC_INTERNAL_TABLES or name in CUTOVER_BOOKKEEPING_TABLES:
        return True
    if name.endswith("_fts") or "_fts_" in name:
        return True
    return False


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone()
    return row is not None


def applied_business_tables(connection: sqlite3.Connection) -> tuple[str, ...]:
    rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    names = []
    for row in rows:
        name = str(row[0] if not isinstance(row, sqlite3.Row) else row["name"])
        if is_excluded_source_table(name):
            continue
        names.append(name)
    return tuple(names)


def _jsonable(value: object) -> object:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, bytes):
        return value.hex()
    return str(value)


def _ordered_columns(connection: sqlite3.Connection, name: str) -> tuple[str, ...]:
    rows = list(connection.execute(f'PRAGMA table_info("{name}")'))
    return tuple(str(row[1]) for row in rows)


def _order_columns(connection: sqlite3.Connection, name: str) -> tuple[str, ...]:
    rows = list(connection.execute(f'PRAGMA table_info("{name}")'))
    primary = sorted(
        ((int(row[5]), str(row[1])) for row in rows if int(row[5]) > 0),
        key=lambda item: item[0],
    )
    if primary:
        return tuple(column for _index, column in primary)
    return tuple(str(row[1]) for row in rows)


def table_content_digest(connection: sqlite3.Connection, name: str) -> dict[str, int | str]:
    columns = _ordered_columns(connection, name)
    if not columns:
        return {
            "row_count": 0,
            "sha256": hashlib.sha256(f"empty-schema:{name}".encode()).hexdigest(),
        }
    order = _order_columns(connection, name)
    quoted = ", ".join(f'"{column}"' for column in columns)
    order_sql = ", ".join(f'"{column}"' for column in order) if order else "rowid"
    try:
        rows = connection.execute(f'SELECT {quoted} FROM "{name}" ORDER BY {order_sql}').fetchall()
    except sqlite3.Error:
        rows = connection.execute(f'SELECT {quoted} FROM "{name}"').fetchall()
        rows = sorted(
            rows,
            key=lambda row: json.dumps(
                {column: _jsonable(row[column]) for column in columns},
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ),
        )
    digest = hashlib.sha256()
    for row in rows:
        payload = {column: _jsonable(row[column]) for column in columns}
        encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        digest.update(encoded.encode())
        digest.update(b"\n")
    return {"row_count": len(rows), "sha256": digest.hexdigest()}


def live_source_manifest(connection: sqlite3.Connection) -> dict[str, dict[str, int | str]]:
    manifest: dict[str, dict[str, int | str]] = {}
    for name in applied_business_tables(connection):
        if not _table_exists(connection, name):
            continue
        manifest[name] = table_content_digest(connection, name)
    return manifest


def source_manifest_fingerprint(manifest: dict[str, dict[str, int | str]]) -> str:
    encoded = json.dumps(manifest, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def normalize_event_text(value: object) -> str:
    return " ".join(str(value or "").split())


def normalize_segments_json(value: object) -> str:
    """Canonicalize JSON object key order only. Do not fold opaque string values."""

    raw = str(value if value is not None else "")
    if not raw.strip():
        return "[]"
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return raw
    return json.dumps(
        _canonicalize_json_keys(parsed),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )


def _canonicalize_json_keys(value: object) -> object:
    if isinstance(value, dict):
        return {str(key): _canonicalize_json_keys(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_canonicalize_json_keys(item) for item in value]
    return value


def manifests_equal(
    left: dict[str, dict[str, int | str]],
    right: dict[str, dict[str, int | str]],
) -> bool:
    if set(left) != set(right):
        return False
    for name in left:
        if left[name] != right[name]:
            return False
    return True
