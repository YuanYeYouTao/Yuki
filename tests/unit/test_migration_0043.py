"""Canonical identity foundation schema and 0043 cutover-descendant proofs."""

from __future__ import annotations

import ast
import re
import sqlite3
from pathlib import Path
from typing import Any
from uuid import uuid1, uuid4

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, event, text
from tests.unit.test_migration_0021 import _config

from qq_ai_bot.identity.canonical_extension_schema import C6_OWNERSHIP_TABLES
from qq_ai_bot.identity.canonical_ownership_schema import C5_OWNERSHIP_TABLES
from qq_ai_bot.identity.db_models import (
    CANONICAL_IDENTITY_CREATE_ORDER,
    CANONICAL_IDENTITY_TABLES,
    IdentityRuntimeStateModel,
    seed_identity_runtime_state_v1,
)
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.metadata import Base

_FORBIDDEN_TABLES = {
    "yuki",
    "yukis",
    "yuki_self",
    "yukiself",
    "gateway_connections",
    "identity_cutover_manifests",
    "identity_cutover_runs",
}
_REQUIRED_CONSTRAINT_FRAGMENTS = (
    "ck_persons_id",
    "ck_persons_enabled",
    "ck_persons_revision",
    "uq_identity_bindings_platform_account",
    "ck_identity_bindings_status",
    "ck_spaces_id",
    "ck_spaces_autonomous_enabled",
    "ck_spaces_require_mention",
    "uq_space_bindings_platform_space",
    "ck_space_bindings_status",
    "uq_presences_platform_account",
    "ck_presences_ingest_eligible",
    "ck_identity_runtime_state_singleton",
    "ck_identity_runtime_state_state",
    "ck_identity_runtime_state_epoch",
    "ck_identity_backfill_runs_mode",
    "ck_identity_backfill_runs_status",
    "ck_identity_backfill_runs_lifecycle",
    "uq_identity_conflicts_subject",
    "ck_identity_conflicts_kind",
    "ck_identity_conflicts_status",
    "ck_identity_conflicts_lifecycle",
)


def _tables(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }


def _schema_dump(path: Path) -> dict[tuple[str, str], str]:
    with sqlite3.connect(path) as connection:
        return {
            (str(kind), str(name)): str(sql or "")
            for kind, name, sql in connection.execute(
                "SELECT type, name, sql FROM sqlite_master "
                "WHERE name NOT LIKE 'sqlite_%' "
                "ORDER BY type, name"
            )
        }


def _normalized_schema(path: Path) -> dict[str, Any]:
    """Compare columns, indexes, foreign keys, named constraints, and triggers."""

    with sqlite3.connect(path) as connection:
        tables = sorted(_tables(connection))
        columns: dict[str, list[tuple[object, ...]]] = {}
        indexes: dict[str, list[tuple[object, ...]]] = {}
        foreign_keys: dict[str, list[tuple[object, ...]]] = {}
        constraints: dict[str, tuple[str, ...]] = {}
        triggers = {
            str(name): str(sql or "")
            for name, sql in connection.execute(
                "SELECT name, sql FROM sqlite_master "
                "WHERE type='trigger' AND name NOT LIKE 'sqlite_%' "
                "ORDER BY name"
            )
        }
        for table in tables:
            columns[table] = [
                (row[1], row[2], row[3], row[4], row[5])
                for row in connection.execute(f'PRAGMA table_info("{table}")')
            ]
            foreign_keys[table] = sorted(
                (row[2], row[3], row[4], row[5], row[6], row[7])
                for row in connection.execute(f'PRAGMA foreign_key_list("{table}")')
            )
            indexes[table] = sorted(
                (
                    "auto" if str(row[1]).startswith("sqlite_autoindex_") else str(row[1]),
                    int(row[2]),
                    tuple(item[2] for item in connection.execute(f'PRAGMA index_info("{row[1]}")')),
                )
                for row in connection.execute(f'PRAGMA index_list("{table}")')
            )
            sql = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone()
            constraints[table] = tuple(
                sorted(re.findall(r"CONSTRAINT\s+(\w+)", str(sql[0] or ""), flags=re.I))
            )
        return {
            "tables": tables,
            "columns": columns,
            "indexes": indexes,
            "foreign_keys": foreign_keys,
            "constraints": constraints,
            "triggers": triggers,
        }


def _identity_schema(path: Path) -> dict[tuple[str, str], str]:
    dump = _schema_dump(path)
    return {
        key: sql
        for key, sql in dump.items()
        if key[1] in CANONICAL_IDENTITY_TABLES
        or (
            key[1] not in C5_OWNERSHIP_TABLES
            and key[1] not in C6_OWNERSHIP_TABLES
            and any(table in sql for table in CANONICAL_IDENTITY_TABLES)
        )
    }


def _normalized_identity_schema(path: Path) -> dict[str, Any]:
    full = _normalized_schema(path)
    tables = [name for name in full["tables"] if name in CANONICAL_IDENTITY_TABLES]
    return {
        "tables": tables,
        "columns": {name: full["columns"][name] for name in tables},
        "indexes": {name: full["indexes"][name] for name in tables},
        "foreign_keys": {name: full["foreign_keys"][name] for name in tables},
        "constraints": {name: full["constraints"][name] for name in tables},
    }


def _runtime_state_rows(path: Path) -> list[tuple[object, ...]]:
    with sqlite3.connect(path) as connection:
        return list(
            connection.execute(
                "SELECT id, state, cutover_id, source_fingerprint, completed_at, revision "
                "FROM identity_runtime_state"
            )
        )


def _call_name(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _create_orm_identity_schema(path: Path) -> None:
    engine = create_engine(f"sqlite:///{path.as_posix()}")
    tables = [Base.metadata.tables[name] for name in CANONICAL_IDENTITY_CREATE_ORDER]
    Base.metadata.create_all(engine, tables=tables)
    engine.dispose()


def _upgrade(path: Path, monkeypatch: pytest.MonkeyPatch, revision: str) -> None:
    command.upgrade(_config(path, monkeypatch), revision)


def _downgrade(path: Path, monkeypatch: pytest.MonkeyPatch, revision: str) -> None:
    command.downgrade(_config(path, monkeypatch), revision)


def test_0005_does_not_create_canonical_identity_tables(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "at-0005.db"
    _upgrade(path, monkeypatch, "0005")
    with sqlite3.connect(path) as connection:
        assert not (set(CANONICAL_IDENTITY_TABLES) & _tables(connection))


def test_0042_does_not_create_canonical_identity_tables(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "at-0042.db"
    _upgrade(path, monkeypatch, "0042")
    with sqlite3.connect(path) as connection:
        tables = _tables(connection)
        assert not (set(CANONICAL_IDENTITY_TABLES) & tables)
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == ("0042",)


def test_fresh_upgrade_head_creates_canonical_identity_foundation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "fresh-head.db"
    _upgrade(path, monkeypatch, "head")
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == ("0047",)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        tables = _tables(connection)
        assert set(CANONICAL_IDENTITY_TABLES) <= tables
        assert not (_FORBIDDEN_TABLES & tables)
        schema_sql = "\n".join(
            str(row[0] or "") for row in connection.execute("SELECT sql FROM sqlite_master")
        )
        for fragment in _REQUIRED_CONSTRAINT_FRAGMENTS:
            assert fragment in schema_sql
        row = connection.execute(
            "SELECT id, state, cutover_id, source_fingerprint, completed_at, revision "
            "FROM identity_runtime_state"
        ).fetchall()
        assert row == [(1, "v1", None, None, None, 1)]


def test_upgrade_from_real_0042_schema_to_head(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "from-0042.db"
    _upgrade(path, monkeypatch, "0042")
    before = _schema_dump(path)
    _upgrade(path, monkeypatch, "head")
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == ("0047",)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert set(CANONICAL_IDENTITY_TABLES) <= _tables(connection)
        assert connection.execute("SELECT state FROM identity_runtime_state").fetchall() == [
            ("v1",)
        ]
    after = _schema_dump(path)
    preserved = {
        "alembic_version",
        "chat_events",
        "conversation_scopes",
        "people",
        "groups",
        "person_aliases",
        "memberships",
        "person_relationships",
        "relationship_events",
        "relationship_jobs",
        "person_time_settings",
        "person_speech_preferences",
        "memory_facts",
        *C6_OWNERSHIP_TABLES,
    }
    assert all(after[key] == sql for key, sql in before.items() if key[1] not in preserved)


def test_fresh_head_and_0042_to_head_schemas_are_equivalent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fresh = tmp_path / "fresh.db"
    upgraded = tmp_path / "from-0042.db"
    _upgrade(fresh, monkeypatch, "head")
    _upgrade(upgraded, monkeypatch, "0042")
    _upgrade(upgraded, monkeypatch, "head")
    assert _normalized_schema(fresh) == _normalized_schema(upgraded)
    assert _identity_schema(fresh) == _identity_schema(upgraded)


def test_downgrade_0043_removes_only_identity_tables(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = tmp_path / "expected-0042.db"
    path = tmp_path / "downgrade.db"
    _upgrade(expected, monkeypatch, "0042")
    _upgrade(path, monkeypatch, "head")
    _downgrade(path, monkeypatch, "0042")
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == ("0042",)
        assert not (set(CANONICAL_IDENTITY_TABLES) & _tables(connection))
    assert _normalized_schema(path) == _normalized_schema(expected)


def test_orm_metadata_matches_0043_identity_schema(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migrated = tmp_path / "migrated.db"
    orm = tmp_path / "orm.db"
    _upgrade(migrated, monkeypatch, "head")
    _create_orm_identity_schema(orm)
    assert set(CANONICAL_IDENTITY_TABLES) <= set(Base.metadata.tables)
    assert _normalized_identity_schema(migrated) == _normalized_identity_schema(orm)
    assert _runtime_state_rows(orm) == [(1, "v1", None, None, None, 1)]
    assert _runtime_state_rows(migrated) == [(1, "v1", None, None, None, 1)]


def test_alembic_heads_is_exactly_0044() -> None:
    config = Config("alembic.ini")
    heads = ScriptDirectory.from_config(config).get_heads()
    assert heads == ["0047"]


def test_fk_cutover_split_is_not_hardcoded_to_current_head() -> None:
    source = Path("migrations/env.py").read_text(encoding="utf-8")
    assert '_FK_CUTOVER_REVISION = "0042"' in source
    assert '"0043"' not in source
    assert "iterate_revisions" in source
    assert "_lineage_contains" in source


def test_0043_is_self_contained_alembic() -> None:
    path = Path("migrations/versions/0043_canonical_identity_foundation.py")
    source = path.read_text(encoding="utf-8")
    assert "qq_ai_bot" not in source
    assert "CANONICAL_IDENTITY" not in source
    assert "Base.metadata" not in source
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert {name.split(".")[0] for name in imported} <= {
        "__future__",
        "collections",
        "sqlalchemy",
        "alembic",
    }
    calls = {_call_name(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call)}
    assert {"create_table", "create_index", "drop_table", "drop_index"} <= calls


def test_0005_excludes_identity_tables_as_frozen_literals() -> None:
    source = Path("migrations/versions/0005_person_centric_v1.py").read_text(encoding="utf-8")
    assert "qq_ai_bot.identity" not in source
    assert "CANONICAL_IDENTITY" not in source
    for table in CANONICAL_IDENTITY_TABLES:
        assert f'"{table}"' in source


def test_canonical_identity_constraint_negatives(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "constraints.db"
    _upgrade(path, monkeypatch, "head")
    person_id = str(uuid4())
    binding_id = str(uuid4())
    space_id = str(uuid4())
    space_binding_id = str(uuid4())
    presence_id = str(uuid4())
    now = "2026-08-24T00:00:00+00:00"

    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            "INSERT INTO persons(id, enabled, revision, created_at, updated_at) "
            "VALUES (?, 1, 1, ?, ?)",
            (person_id, now, now),
        )
        connection.execute(
            "INSERT INTO spaces(id, name, enabled, autonomous_enabled, require_mention, "
            "revision, created_at, updated_at) VALUES (?, '', 1, 1, 1, 1, ?, ?)",
            (space_id, now, now),
        )
        connection.execute(
            "INSERT INTO identity_bindings("
            "id, person_id, platform, external_account_id, display_name, status, "
            "revision, created_at, updated_at"
            ") VALUES (?, ?, 'qq', '1001', '', 'active', 1, ?, ?)",
            (binding_id, person_id, now, now),
        )
        connection.execute(
            "INSERT INTO space_bindings("
            "id, space_id, platform, external_space_id, display_name, status, "
            "revision, created_at, updated_at"
            ") VALUES (?, ?, 'qq', '2001', '', 'active', 1, ?, ?)",
            (space_binding_id, space_id, now, now),
        )
        connection.execute(
            "INSERT INTO presences("
            "id, platform, external_account_id, enabled, ingest_eligible, "
            "revision, created_at, updated_at"
            ") VALUES (?, 'qq', '8000', 1, 1, 1, ?, ?)",
            (presence_id, now, now),
        )
        connection.execute(
            "INSERT INTO identity_conflicts("
            "platform, external_id, subject_kind, conflict_kind, status, created_at, updated_at"
            ") VALUES ('qq', '1001', 'account', 'ambiguous_identity', 'open', ?, ?)",
            (now, now),
        )
        connection.commit()

        negatives = (
            (
                "INSERT INTO persons(id, enabled, revision, created_at, updated_at) "
                "VALUES (?, 1, 1, ?, ?)",
                (str(uuid4()).upper(), now, now),
            ),
            (
                "INSERT INTO persons(id, enabled, revision, created_at, updated_at) "
                "VALUES (?, 1, 1, ?, ?)",
                (str(uuid1()), now, now),
            ),
            (
                "INSERT INTO persons(id, enabled, revision, created_at, updated_at) "
                "VALUES (?, 1, 1, ?, ?)",
                ("not-a-uuid", now, now),
            ),
            (
                "INSERT INTO persons(id, enabled, revision, created_at, updated_at) "
                "VALUES (?, 2, 1, ?, ?)",
                (str(uuid4()), now, now),
            ),
            (
                "INSERT INTO persons(id, enabled, revision, created_at, updated_at) "
                "VALUES (?, 1, 0, ?, ?)",
                (str(uuid4()), now, now),
            ),
            (
                "INSERT INTO identity_bindings("
                "id, person_id, platform, external_account_id, display_name, status, "
                "revision, created_at, updated_at"
                ") VALUES (?, ?, 'qq', '1001', '', 'active', 1, ?, ?)",
                (str(uuid4()), person_id, now, now),
            ),
            (
                "INSERT INTO identity_bindings("
                "id, person_id, platform, external_account_id, display_name, status, "
                "revision, created_at, updated_at"
                ") VALUES (?, ?, '', '1002', '', 'active', 1, ?, ?)",
                (str(uuid4()), person_id, now, now),
            ),
            (
                "INSERT INTO identity_bindings("
                "id, person_id, platform, external_account_id, display_name, status, "
                "revision, created_at, updated_at"
                ") VALUES (?, ?, 'QQ', '1002', '', 'active', 1, ?, ?)",
                (str(uuid4()), person_id, now, now),
            ),
            (
                "INSERT INTO identity_bindings("
                "id, person_id, platform, external_account_id, display_name, status, "
                "revision, created_at, updated_at"
                ") VALUES (?, ?, ' qq', '1002', '', 'active', 1, ?, ?)",
                (str(uuid4()), person_id, now, now),
            ),
            (
                "INSERT INTO identity_bindings("
                "id, person_id, platform, external_account_id, display_name, status, "
                "revision, created_at, updated_at"
                ") VALUES (?, ?, 'qq ', '1002', '', 'active', 1, ?, ?)",
                (str(uuid4()), person_id, now, now),
            ),
            (
                "INSERT INTO identity_bindings("
                "id, person_id, platform, external_account_id, display_name, status, "
                "revision, created_at, updated_at"
                ") VALUES (?, ?, '   ', '1002', '', 'active', 1, ?, ?)",
                (str(uuid4()), person_id, now, now),
            ),
            (
                "INSERT INTO identity_bindings("
                "id, person_id, platform, external_account_id, display_name, status, "
                "revision, created_at, updated_at"
                ") VALUES (?, ?, 'qq', ' 1002', '', 'active', 1, ?, ?)",
                (str(uuid4()), person_id, now, now),
            ),
            (
                "INSERT INTO identity_bindings("
                "id, person_id, platform, external_account_id, display_name, status, "
                "revision, created_at, updated_at"
                ") VALUES (?, ?, 'qq', '1002 ', '', 'active', 1, ?, ?)",
                (str(uuid4()), person_id, now, now),
            ),
            (
                "INSERT INTO identity_bindings("
                "id, person_id, platform, external_account_id, display_name, status, "
                "revision, created_at, updated_at"
                ") VALUES (?, ?, 'qq', '   ', '', 'active', 1, ?, ?)",
                (str(uuid4()), person_id, now, now),
            ),
            (
                "INSERT INTO space_bindings("
                "id, space_id, platform, external_space_id, display_name, status, "
                "revision, created_at, updated_at"
                ") VALUES (?, ?, 'QQ', '2002', '', 'active', 1, ?, ?)",
                (str(uuid4()), space_id, now, now),
            ),
            (
                "INSERT INTO presences("
                "id, platform, external_account_id, enabled, ingest_eligible, "
                "revision, created_at, updated_at"
                ") VALUES (?, ' qq', '8002', 1, 1, 1, ?, ?)",
                (str(uuid4()), now, now),
            ),
            (
                "INSERT INTO identity_bindings("
                "id, person_id, platform, external_account_id, display_name, status, "
                "revision, created_at, updated_at"
                ") VALUES (?, ?, 'qq', '1002', '', 'conflict', 1, ?, ?)",
                (str(uuid4()), person_id, now, now),
            ),
            (
                "INSERT INTO identity_bindings("
                "id, person_id, platform, external_account_id, display_name, status, "
                "revision, created_at, updated_at"
                ") VALUES (?, ?, 'qq', '1002', '', 'active', 1, ?, ?)",
                (str(uuid4()), str(uuid4()), now, now),
            ),
            (
                "INSERT INTO space_bindings("
                "id, space_id, platform, external_space_id, display_name, status, "
                "revision, created_at, updated_at"
                ") VALUES (?, ?, 'qq', '2001', '', 'active', 1, ?, ?)",
                (str(uuid4()), space_id, now, now),
            ),
            (
                "INSERT INTO presences("
                "id, platform, external_account_id, enabled, ingest_eligible, "
                "revision, created_at, updated_at"
                ") VALUES (?, 'qq', '8000', 1, 1, 1, ?, ?)",
                (str(uuid4()), now, now),
            ),
            (
                "INSERT INTO presences("
                "id, platform, external_account_id, enabled, ingest_eligible, "
                "revision, created_at, updated_at"
                ") VALUES (?, 'qq', '8001', 1, 2, 1, ?, ?)",
                (str(uuid4()), now, now),
            ),
            (
                "INSERT INTO identity_runtime_state("
                "id, state, revision, created_at, updated_at"
                ") VALUES (2, 'v1', 1, ?, ?)",
                (now, now),
            ),
            (
                "UPDATE identity_runtime_state SET state='v3' WHERE id=1",
                (),
            ),
            (
                "UPDATE identity_runtime_state SET cutover_id=? WHERE id=1",
                (str(uuid4()),),
            ),
            (
                "UPDATE identity_runtime_state SET state='v2' WHERE id=1",
                (),
            ),
            (
                "UPDATE identity_runtime_state SET state='v2', cutover_id=?, "
                "source_fingerprint='', completed_at=? WHERE id=1",
                (str(uuid4()), now),
            ),
            (
                "UPDATE identity_runtime_state SET state='v2', cutover_id=?, "
                "source_fingerprint='fp', completed_at=? WHERE id=1",
                (str(uuid1()), now),
            ),
            (
                "INSERT INTO identity_backfill_runs("
                "mode, status, processed_count, persons_count, identity_bindings_count, "
                "spaces_count, space_bindings_count, presences_count, conflicts_count, "
                "skipped_count, created_at, updated_at"
                ") VALUES ('replay', 'pending', 0, 0, 0, 0, 0, 0, 0, 0, ?, ?)",
                (now, now),
            ),
            (
                "INSERT INTO identity_backfill_runs("
                "mode, status, processed_count, persons_count, identity_bindings_count, "
                "spaces_count, space_bindings_count, presences_count, conflicts_count, "
                "skipped_count, created_at, updated_at"
                ") VALUES ('dry_run', 'pending', -1, 0, 0, 0, 0, 0, 0, 0, ?, ?)",
                (now, now),
            ),
            (
                "INSERT INTO identity_backfill_runs("
                "mode, status, processed_count, persons_count, identity_bindings_count, "
                "spaces_count, space_bindings_count, presences_count, conflicts_count, "
                "skipped_count, started_at, created_at, updated_at"
                ") VALUES ('dry_run', 'pending', 0, 0, 0, 0, 0, 0, 0, 0, ?, ?, ?)",
                (now, now, now),
            ),
            (
                "INSERT INTO identity_backfill_runs("
                "mode, status, processed_count, persons_count, identity_bindings_count, "
                "spaces_count, space_bindings_count, presences_count, conflicts_count, "
                "skipped_count, started_at, finished_at, created_at, updated_at"
                ") VALUES ('apply', 'failed', 0, 0, 0, 0, 0, 0, 0, 0, ?, ?, ?, ?)",
                (now, now, now, now),
            ),
            (
                "INSERT INTO identity_conflicts("
                "platform, external_id, subject_kind, conflict_kind, status, created_at, updated_at"
                ") VALUES ('qq', '1001', 'account', 'ambiguous_identity', 'open', ?, ?)",
                (now, now),
            ),
            (
                "INSERT INTO identity_conflicts("
                "platform, external_id, subject_kind, conflict_kind, status, created_at, updated_at"
                ") VALUES ('', '1002', 'account', 'unclassified', 'open', ?, ?)",
                (now, now),
            ),
            (
                "INSERT INTO identity_conflicts("
                "platform, external_id, subject_kind, conflict_kind, status, created_at, updated_at"
                ") VALUES ('qq', '1002', 'account', 'forged_binding', 'open', ?, ?)",
                (now, now),
            ),
            (
                "INSERT INTO identity_conflicts("
                "platform, external_id, subject_kind, conflict_kind, status, resolved_at, "
                "created_at, updated_at"
                ") VALUES ('qq', '1003', 'account', 'unclassified', 'open', ?, ?, ?)",
                (now, now, now),
            ),
            (
                "INSERT INTO identity_conflicts("
                "platform, external_id, subject_kind, conflict_kind, status, created_at, updated_at"
                ") VALUES ('qq', '1004', 'account', 'unclassified', 'resolved', ?, ?)",
                (now, now),
            ),
            (
                "INSERT INTO identity_conflicts("
                "platform, external_id, subject_kind, conflict_kind, status, created_at, updated_at"
                ") VALUES ('QQ', '1005', 'account', 'unclassified', 'open', ?, ?)",
                (now, now),
            ),
        )
        for sql, params in negatives:
            connection.execute("BEGIN")
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(sql, params)
            connection.rollback()

        connection.execute(
            "UPDATE identity_runtime_state SET state='v2', cutover_id=?, "
            "source_fingerprint='cutover-fingerprint', completed_at=? WHERE id=1",
            (str(uuid4()), now),
        )
        assert connection.execute("SELECT state FROM identity_runtime_state").fetchone() == ("v2",)
        connection.execute(
            "UPDATE identity_runtime_state SET state='v1', cutover_id=NULL, "
            "source_fingerprint=NULL, completed_at=NULL WHERE id=1"
        )
        connection.execute(
            "INSERT INTO identity_backfill_runs("
            "mode, status, processed_count, persons_count, identity_bindings_count, "
            "spaces_count, space_bindings_count, presences_count, conflicts_count, "
            "skipped_count, created_at, updated_at"
            ") VALUES ('dry_run', 'pending', 0, 0, 0, 0, 0, 0, 0, 0, ?, ?)",
            (now, now),
        )
        mixed_id = "Acct-ID_OK"
        connection.execute(
            "INSERT INTO identity_bindings("
            "id, person_id, platform, external_account_id, display_name, status, "
            "revision, created_at, updated_at"
            ") VALUES (?, ?, 'qq', ?, '', 'active', 1, ?, ?)",
            (str(uuid4()), person_id, mixed_id, now, now),
        )
        connection.execute(
            "INSERT INTO identity_conflicts("
            "platform, external_id, subject_kind, conflict_kind, status, resolved_at, "
            "created_at, updated_at"
            ") VALUES ('qq', ?, 'account', 'unclassified', 'resolved', ?, ?, ?)",
            (mixed_id, now, now, now),
        )
        connection.commit()
        assert connection.execute("SELECT COUNT(*) FROM identity_backfill_runs").fetchone() == (1,)
        assert connection.execute(
            "SELECT external_account_id FROM identity_bindings WHERE external_account_id=?",
            (mixed_id,),
        ).fetchone() == (mixed_id,)
        assert connection.execute(
            "SELECT status, resolved_at IS NOT NULL FROM identity_conflicts WHERE external_id=?",
            (mixed_id,),
        ).fetchone() == ("resolved", 1)


def test_runtime_state_seed_listener_is_table_scoped() -> None:
    table = IdentityRuntimeStateModel.__table__
    assert event.contains(table, "after_create", seed_identity_runtime_state_v1)
    for name, other in Base.metadata.tables.items():
        if name == "identity_runtime_state":
            continue
        assert not event.contains(other, "after_create", seed_identity_runtime_state_v1)


def test_orm_create_all_seeds_exactly_one_v1_and_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "orm-seed.db"
    _create_orm_identity_schema(path)
    assert _runtime_state_rows(path) == [(1, "v1", None, None, None, 1)]
    _create_orm_identity_schema(path)
    assert _runtime_state_rows(path) == [(1, "v1", None, None, None, 1)]


@pytest.mark.asyncio
async def test_database_create_schema_seeds_exactly_one_v1(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'fresh.db').as_posix()}")
    try:
        await database.create_schema()
        await database.create_schema()
        async with database.sessions() as session:
            rows = (
                await session.execute(text("SELECT id, state FROM identity_runtime_state"))
            ).all()
        assert [(int(row[0]), str(row[1])) for row in rows] == [(1, "v1")]
    finally:
        await database.close()


def test_0005_does_not_seed_identity_runtime_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "0005-no-seed.db"
    _upgrade(path, monkeypatch, "0005")
    with sqlite3.connect(path) as connection:
        tables = _tables(connection)
        assert "identity_runtime_state" not in tables
        assert (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE name='identity_runtime_state'"
            ).fetchone()
            is None
        )
