"""Test-only erase of canonical identity backfill results.

Production packages and CLI must not import this module. The target must
be proven to be a pytest sqlite file before any DELETE/UPDATE runs.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from qq_ai_bot.identity.backfill_repository import (
    _column_exists,
    _table_exists,
    connect_sqlite,
    sqlite_path_from_url,
)
from qq_ai_bot.identity.inventory import SHADOW_FILL_SPECS

_PRODUCTION_NAMES = frozenset({"qq_ai_bot.db", "yuki.db"})


def assert_test_database(path: Path) -> Path:
    """Refuse anything that does not look like an isolated pytest sqlite file."""

    if os.environ.get("PYTEST_CURRENT_TEST") is None:
        raise RuntimeError("canonical identity erase is only allowed under pytest")
    resolved = path.resolve()
    if resolved.name in _PRODUCTION_NAMES:
        raise RuntimeError("refusing to erase a production-looking sqlite file")
    text = str(resolved).casefold()
    test_markers = ("pytest", "test", "tmp", "temp")
    if not any(marker in text for marker in test_markers):
        raise RuntimeError("refusing to erase a sqlite file outside the test sandbox")
    if not resolved.is_file():
        raise RuntimeError("identity erase target is not a sqlite file")
    return resolved


def erase_canonical_identity_backfill(database_url: str) -> None:
    """Null C7 shadows and delete canonical foundation rows. Legacy rows stay."""

    path = assert_test_database(sqlite_path_from_url(database_url))
    connection = connect_sqlite(path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        _erase(connection)
        connection.execute("COMMIT")
        violations = list(connection.execute("PRAGMA foreign_key_check"))
        if violations:
            raise RuntimeError("identity erase left foreign-key violations")
    except Exception:
        try:
            connection.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        connection.close()


def _erase(connection: sqlite3.Connection) -> None:
    for spec in SHADOW_FILL_SPECS:
        if not _table_exists(connection, spec.table):
            continue
        if not _column_exists(connection, spec.table, spec.column):
            continue
        connection.execute(f'UPDATE "{spec.table}" SET "{spec.column}" = NULL')
    for table in (
        "identity_bindings",
        "space_bindings",
        "identity_conflicts",
        "identity_backfill_runs",
        "persons",
        "spaces",
        "presences",
    ):
        if _table_exists(connection, table):
            connection.execute(f'DELETE FROM "{table}"')
