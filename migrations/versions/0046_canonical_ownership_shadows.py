"""Add nullable person and space ownership shadows.

Revision ID: 0046
Revises: 0045
Create Date: 2026-08-24

This revision is frozen and self-contained. It must not import application
modules or create tables from current ORM metadata.

Ownership shadows are added with ADD COLUMN ... REFERENCES ... on nullable
columns without a non-null default. SQLite records those foreign keys in
PRAGMA foreign_key_list and enforces child writes plus parent DELETE /
primary-key UPDATE while foreign_keys=ON. Triggers keep UUID4 and
cross-column shape rules; they are not a substitute for those foreign keys.
SQLite 3.35+ is required for DROP COLUMN on downgrade.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0046"
down_revision: str | None = "0045"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_FK_RESTRICT = "ON UPDATE RESTRICT ON DELETE RESTRICT"
_UUID4_GLOB = (
    "[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]-"
    "[0-9a-f][0-9a-f][0-9a-f][0-9a-f]-4[0-9a-f][0-9a-f][0-9a-f]-"
    "[89ab][0-9a-f][0-9a-f][0-9a-f]-"
    "[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]"
    "[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]"
)
_OWNERSHIP_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("people", "canonical_person_id", "persons"),
    ("groups", "canonical_space_id", "spaces"),
    ("person_aliases", "canonical_person_id", "persons"),
    ("person_aliases", "canonical_space_id", "spaces"),
    ("memberships", "canonical_person_id", "persons"),
    ("memberships", "canonical_space_id", "spaces"),
    ("person_relationships", "canonical_person_id", "persons"),
    ("relationship_events", "canonical_person_id", "persons"),
    ("relationship_jobs", "canonical_person_id", "persons"),
    ("person_time_settings", "canonical_person_id", "persons"),
    ("person_speech_preferences", "canonical_person_id", "persons"),
    ("memory_facts", "canonical_subject_person_id", "persons"),
    ("memory_facts", "canonical_subject_space_id", "spaces"),
    ("memory_facts", "canonical_visibility_person_id", "persons"),
    ("memory_facts", "canonical_visibility_space_id", "spaces"),
)
_OWNERSHIP_INDEXES: tuple[str, ...] = tuple(
    f"ix_{table}_{column}" for table, column, _parent in _OWNERSHIP_COLUMNS
)


def _optional_uuid4_sql(column: str) -> str:
    return (
        f"({column} IS NULL OR ("
        f"length({column}) = 36 AND {column} = lower({column}) "
        f"AND {column} GLOB '{_UUID4_GLOB}'"
        f"))"
    )


def _uuid4_columns_valid_sql(columns: tuple[str, ...]) -> str:
    return " AND ".join(_optional_uuid4_sql(f"NEW.{column}") for column in columns)


def _alias_shadow_valid_sql() -> str:
    return (
        f"{_uuid4_columns_valid_sql(('canonical_person_id', 'canonical_space_id'))} AND ("
        "NEW.canonical_space_id IS NULL OR NEW.group_scope != ''"
        ")"
    )


def _memory_facts_shadow_valid_sql() -> str:
    return (
        f"{
            _uuid4_columns_valid_sql(
                (
                    'canonical_subject_person_id',
                    'canonical_subject_space_id',
                    'canonical_visibility_person_id',
                    'canonical_visibility_space_id',
                )
            )
        } AND ("
        "NEW.scope_type != 'self' OR ("
        "NEW.canonical_subject_person_id IS NULL AND "
        "NEW.canonical_subject_space_id IS NULL"
        ")) AND ("
        "NEW.scope_type = 'self' OR ("
        "NEW.canonical_visibility_person_id IS NULL AND "
        "NEW.canonical_visibility_space_id IS NULL"
        ")) AND ("
        "NEW.canonical_visibility_person_id IS NULL OR NEW.visibility_type = 'private'"
        ") AND ("
        "NEW.canonical_visibility_space_id IS NULL OR NEW.visibility_type = 'group'"
        ") AND NOT ("
        "NEW.canonical_visibility_person_id IS NOT NULL AND "
        "NEW.canonical_visibility_space_id IS NOT NULL"
        ") AND ("
        "NEW.scope_type != 'person' OR NEW.canonical_subject_space_id IS NULL"
        ") AND ("
        "NEW.scope_type != 'group' OR NEW.canonical_subject_person_id IS NULL"
        ")"
    )


def _trigger_pair(
    table: str,
    columns: tuple[str, ...],
    valid_sql: str,
    message: str,
) -> tuple[str, str]:
    column_list = ", ".join(columns)
    insert_sql = f"""
CREATE TRIGGER trg_{table}_ownership_shadow_insert
BEFORE INSERT ON {table}
BEGIN
    SELECT RAISE(ABORT, '{message}')
    WHERE NOT ({valid_sql});
END
""".strip()
    update_sql = f"""
CREATE TRIGGER trg_{table}_ownership_shadow_update
BEFORE UPDATE OF {column_list} ON {table}
BEGIN
    SELECT RAISE(ABORT, '{message}')
    WHERE NOT ({valid_sql});
END
""".strip()
    return insert_sql, update_sql


def _c5_trigger_sql() -> tuple[str, ...]:
    groups: dict[str, list[str]] = {}
    for table, column, _parent in _OWNERSHIP_COLUMNS:
        groups.setdefault(table, []).append(column)
    statements: list[str] = []
    for table, columns in groups.items():
        column_tuple = tuple(columns)
        if table == "memory_facts":
            valid = _memory_facts_shadow_valid_sql()
            message = "invalid memory fact ownership shadow"
        elif table == "person_aliases":
            valid = _alias_shadow_valid_sql()
            message = "invalid person alias ownership shadow"
        else:
            valid = _uuid4_columns_valid_sql(column_tuple)
            message = f"invalid {table} ownership shadow"
        statements.extend(_trigger_pair(table, column_tuple, valid, message))
    return tuple(statements)


_C5_TRIGGER_SQL: tuple[str, ...] = _c5_trigger_sql()
_C5_TRIGGER_NAMES: tuple[str, ...] = tuple(
    f"trg_{table}_ownership_shadow_{action}"
    for table in dict.fromkeys(item[0] for item in _OWNERSHIP_COLUMNS)
    for action in ("insert", "update")
)


def _require_sqlite_column_alter() -> None:
    connection = op.get_bind()
    raw = connection.exec_driver_sql("SELECT sqlite_version()").scalar_one()
    parts = tuple(int(part) for part in str(raw).split(".")[:3])
    if parts < (3, 35, 0):
        raise RuntimeError(
            f"0046 requires SQLite 3.35+ for ADD/DROP COLUMN with foreign_keys=ON, got {raw}"
        )
    if int(connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one()) != 1:
        raise RuntimeError("0046 requires PRAGMA foreign_keys=ON")


def upgrade() -> None:
    """Add nullable ownership shadows, non-unique indexes, and shape triggers."""

    _require_sqlite_column_alter()
    for table, column, parent in _OWNERSHIP_COLUMNS:
        op.execute(
            sa.text(
                f"ALTER TABLE {table} ADD COLUMN {column} VARCHAR(36) "
                f"REFERENCES {parent}(id) {_FK_RESTRICT}"
            )
        )
        op.execute(sa.text(f"CREATE INDEX ix_{table}_{column} ON {table} ({column})"))
    for statement in _C5_TRIGGER_SQL:
        op.execute(statement)


def downgrade() -> None:
    """Remove only C5 shadows, leaving 0045 rows and C4 ledger shadows intact."""

    _require_sqlite_column_alter()
    for name in _C5_TRIGGER_NAMES:
        op.execute(f"DROP TRIGGER IF EXISTS {name}")
    for table, column, _parent in reversed(_OWNERSHIP_COLUMNS):
        op.execute(sa.text(f"DROP INDEX IF EXISTS ix_{table}_{column}"))
        op.execute(sa.text(f"ALTER TABLE {table} DROP COLUMN {column}"))
