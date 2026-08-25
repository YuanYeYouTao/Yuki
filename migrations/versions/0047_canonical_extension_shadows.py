"""Add nullable extension ownership and correlation shadows.

Revision ID: 0047
Revises: 0046
Create Date: 2026-08-24

This revision is frozen and self-contained. It must not import application
modules or create tables from current ORM metadata.

Extension shadows are added with ADD COLUMN ... REFERENCES ... on nullable
columns without a non-null default. SQLite records those foreign keys in
PRAGMA foreign_key_list and enforces child writes plus parent DELETE /
primary-key UPDATE while foreign_keys=ON. Triggers keep UUID4 and
mutually exclusive scope/target shape rules, including legacy
discriminators on UPDATE; they do not require v1 rows to populate
shadows. This revision also replaces two 0046 C5 update triggers so
group_scope / scope_type / visibility_type cannot bypass those guards.
Downgrade restores the 0046 C5 trigger text. SQLite 3.35+ is required
for DROP COLUMN on downgrade.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0047"
down_revision: str | None = "0046"
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
_EXTENSION_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("automations", "canonical_creator_person_id", "persons"),
    ("automations", "canonical_target_person_id", "persons"),
    ("automations", "canonical_target_space_id", "spaces"),
    ("automations", "canonical_presence_id", "presences"),
    ("plugin_config_values", "canonical_person_id", "persons"),
    ("plugin_config_values", "canonical_space_id", "spaces"),
    ("plugin_state", "canonical_person_id", "persons"),
    ("plugin_agent_sessions", "canonical_owner_person_id", "persons"),
    ("plugin_agent_sessions", "canonical_space_id", "spaces"),
    ("plugin_agent_messages", "canonical_sender_person_id", "persons"),
    ("plugin_background_target_grants", "canonical_target_person_id", "persons"),
    ("plugin_background_target_grants", "canonical_target_space_id", "spaces"),
    ("plugin_background_target_grants", "canonical_created_by_person_id", "persons"),
    ("plugin_background_target_grants", "canonical_presence_id", "presences"),
    ("plugin_notification_outbox", "canonical_target_person_id", "persons"),
    ("plugin_notification_outbox", "canonical_target_space_id", "spaces"),
    ("plugin_notification_outbox", "canonical_conversation_id", "canonical_conversations"),
    ("plugin_notification_outbox", "canonical_presence_id", "presences"),
    ("plugin_background_turn_jobs", "canonical_target_person_id", "persons"),
    ("plugin_background_turn_jobs", "canonical_target_space_id", "spaces"),
    ("plugin_background_turn_jobs", "canonical_conversation_id", "canonical_conversations"),
    ("plugin_background_turn_jobs", "canonical_presence_id", "presences"),
    ("runtime_config_overrides", "canonical_person_id", "persons"),
    ("runtime_config_overrides", "canonical_space_id", "spaces"),
    ("emoji_assets", "canonical_first_seen_person_id", "persons"),
    ("emoji_assets", "canonical_first_seen_space_id", "spaces"),
    ("emoji_scope_states", "canonical_space_id", "spaces"),
    ("emoji_usage_events", "canonical_actor_person_id", "persons"),
    ("emoji_usage_events", "canonical_space_id", "spaces"),
    ("speech_generations", "canonical_conversation_id", "canonical_conversations"),
    ("tool_invocations", "canonical_conversation_id", "canonical_conversations"),
    ("web_search_runs", "canonical_conversation_id", "canonical_conversations"),
    ("model_invocations", "canonical_conversation_id", "canonical_conversations"),
    ("runtime_turn_observations", "canonical_conversation_id", "canonical_conversations"),
    ("runtime_turn_observations", "canonical_person_id", "persons"),
    ("runtime_turn_observations", "canonical_space_id", "spaces"),
    ("reply_effect_events", "canonical_conversation_id", "canonical_conversations"),
)
_EXTENSION_INDEXES: tuple[str, ...] = tuple(
    f"ix_{table}_{column}" for table, column, _parent in _EXTENSION_COLUMNS
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


def _not_both_sql(left: str, right: str) -> str:
    return f"NOT (NEW.{left} IS NOT NULL AND NEW.{right} IS NOT NULL)"


def _scoped_person_space_sql(
    *,
    scope_column: str,
    person_column: str,
    space_column: str,
    person_scope: str,
    space_scope: str,
    empty_scope: str | None = None,
) -> str:
    clauses = [
        _uuid4_columns_valid_sql((person_column, space_column)),
        _not_both_sql(person_column, space_column),
        f"(NEW.{person_column} IS NULL OR NEW.{scope_column} = '{person_scope}')",
        f"(NEW.{space_column} IS NULL OR NEW.{scope_column} = '{space_scope}')",
    ]
    if empty_scope is not None:
        clauses.append(
            f"(NEW.{scope_column} != '{empty_scope}' OR ("
            f"NEW.{person_column} IS NULL AND NEW.{space_column} IS NULL))"
        )
    return " AND ".join(f"({clause})" for clause in clauses)


def _automation_shadow_valid_sql() -> str:
    return f"{
        _uuid4_columns_valid_sql(
            (
                'canonical_creator_person_id',
                'canonical_target_person_id',
                'canonical_target_space_id',
                'canonical_presence_id',
            )
        )
    } AND {_not_both_sql('canonical_target_person_id', 'canonical_target_space_id')}"


def _plugin_session_shadow_valid_sql() -> str:
    return (
        f"{_uuid4_columns_valid_sql(('canonical_owner_person_id', 'canonical_space_id'))} AND "
        "(NEW.canonical_space_id IS NULL OR NEW.scope_type = 'group')"
    )


def _plugin_target_shadow_valid_sql(extra: tuple[str, ...]) -> str:
    columns = ("canonical_target_person_id", "canonical_target_space_id", *extra)
    return (
        f"{_uuid4_columns_valid_sql(columns)} AND "
        f"{_not_both_sql('canonical_target_person_id', 'canonical_target_space_id')} AND "
        "(NEW.canonical_target_person_id IS NULL OR NEW.target_type = 'private') AND "
        "(NEW.canonical_target_space_id IS NULL OR NEW.target_type = 'group')"
    )


def _emoji_scope_shadow_valid_sql() -> str:
    return (
        f"{_optional_uuid4_sql('NEW.canonical_space_id')} AND "
        "(NEW.canonical_space_id IS NULL OR NEW.scope_type = 'group') AND "
        "(NEW.scope_type != 'global' OR NEW.canonical_space_id IS NULL)"
    )


def _runtime_turn_shadow_valid_sql() -> str:
    return (
        _scoped_person_space_sql(
            scope_column="scope_type",
            person_column="canonical_person_id",
            space_column="canonical_space_id",
            person_scope="private",
            space_scope="group",
        )
        + " AND "
        + _optional_uuid4_sql("NEW.canonical_conversation_id")
    )


_C6_SHAPE_DISCRIMINATORS: dict[str, tuple[str, ...]] = {
    "plugin_config_values": ("scope_type",),
    "runtime_config_overrides": ("scope_type",),
    "plugin_agent_sessions": ("scope_type",),
    "plugin_background_target_grants": ("target_type",),
    "plugin_notification_outbox": ("target_type",),
    "plugin_background_turn_jobs": ("target_type",),
    "emoji_scope_states": ("scope_type",),
    "runtime_turn_observations": ("scope_type",),
}


def _trigger_pair(
    table: str,
    columns: tuple[str, ...],
    valid_sql: str,
    message: str,
) -> tuple[str, str]:
    column_list = ", ".join(columns)
    insert_sql = f"""
CREATE TRIGGER trg_{table}_extension_shadow_insert
BEFORE INSERT ON {table}
BEGIN
    SELECT RAISE(ABORT, '{message}')
    WHERE NOT ({valid_sql});
END
""".strip()
    update_sql = f"""
CREATE TRIGGER trg_{table}_extension_shadow_update
BEFORE UPDATE OF {column_list} ON {table}
BEGIN
    SELECT RAISE(ABORT, '{message}')
    WHERE NOT ({valid_sql});
END
""".strip()
    return insert_sql, update_sql


def _c6_trigger_sql() -> tuple[str, ...]:
    groups: dict[str, list[str]] = {}
    for table, column, _parent in _EXTENSION_COLUMNS:
        groups.setdefault(table, []).append(column)
    statements: list[str] = []
    for table, columns in groups.items():
        column_tuple = tuple(columns)
        if table == "automations":
            valid = _automation_shadow_valid_sql()
        elif table in {"plugin_config_values", "runtime_config_overrides"}:
            valid = _scoped_person_space_sql(
                scope_column="scope_type",
                person_column="canonical_person_id",
                space_column="canonical_space_id",
                person_scope="user",
                space_scope="group",
                empty_scope="global",
            )
        elif table == "plugin_agent_sessions":
            valid = _plugin_session_shadow_valid_sql()
        elif table == "plugin_background_target_grants":
            valid = _plugin_target_shadow_valid_sql(
                ("canonical_created_by_person_id", "canonical_presence_id")
            )
        elif table in {"plugin_notification_outbox", "plugin_background_turn_jobs"}:
            valid = _plugin_target_shadow_valid_sql(
                ("canonical_conversation_id", "canonical_presence_id")
            )
        elif table == "emoji_scope_states":
            valid = _emoji_scope_shadow_valid_sql()
        elif table == "runtime_turn_observations":
            valid = _runtime_turn_shadow_valid_sql()
        else:
            valid = _uuid4_columns_valid_sql(column_tuple)
        watch = (*column_tuple, *_C6_SHAPE_DISCRIMINATORS.get(table, ()))
        statements.extend(_trigger_pair(table, watch, valid, f"invalid {table} extension shadow"))
    return tuple(statements)


_C6_TRIGGER_SQL: tuple[str, ...] = _c6_trigger_sql()
_C6_TRIGGER_NAMES: tuple[str, ...] = tuple(
    f"trg_{table}_extension_shadow_{action}"
    for table in dict.fromkeys(item[0] for item in _EXTENSION_COLUMNS)
    for action in ("insert", "update")
)


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


def _c5_update_trigger(table: str, columns: tuple[str, ...], valid_sql: str, message: str) -> str:
    column_list = ", ".join(columns)
    return f"""
CREATE TRIGGER trg_{table}_ownership_shadow_update
BEFORE UPDATE OF {column_list} ON {table}
BEGIN
    SELECT RAISE(ABORT, '{message}')
    WHERE NOT ({valid_sql});
END
""".strip()


_C5_HARDENED_UPDATE_SQL: tuple[str, ...] = (
    _c5_update_trigger(
        "person_aliases",
        ("canonical_person_id", "canonical_space_id", "group_scope"),
        _alias_shadow_valid_sql(),
        "invalid person alias ownership shadow",
    ),
    _c5_update_trigger(
        "memory_facts",
        (
            "canonical_subject_person_id",
            "canonical_subject_space_id",
            "canonical_visibility_person_id",
            "canonical_visibility_space_id",
            "scope_type",
            "visibility_type",
        ),
        _memory_facts_shadow_valid_sql(),
        "invalid memory fact ownership shadow",
    ),
)
_C5_LEGACY_UPDATE_SQL: tuple[str, ...] = (
    _c5_update_trigger(
        "person_aliases",
        ("canonical_person_id", "canonical_space_id"),
        _alias_shadow_valid_sql(),
        "invalid person alias ownership shadow",
    ),
    _c5_update_trigger(
        "memory_facts",
        (
            "canonical_subject_person_id",
            "canonical_subject_space_id",
            "canonical_visibility_person_id",
            "canonical_visibility_space_id",
        ),
        _memory_facts_shadow_valid_sql(),
        "invalid memory fact ownership shadow",
    ),
)
_C5_REPLACED_UPDATE_NAMES: tuple[str, ...] = (
    "trg_person_aliases_ownership_shadow_update",
    "trg_memory_facts_ownership_shadow_update",
)


def _require_sqlite_column_alter() -> None:
    connection = op.get_bind()
    raw = connection.exec_driver_sql("SELECT sqlite_version()").scalar_one()
    parts = tuple(int(part) for part in str(raw).split(".")[:3])
    if parts < (3, 35, 0):
        raise RuntimeError(
            f"0047 requires SQLite 3.35+ for ADD/DROP COLUMN with foreign_keys=ON, got {raw}"
        )
    if int(connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one()) != 1:
        raise RuntimeError("0047 requires PRAGMA foreign_keys=ON")


def upgrade() -> None:
    """Add nullable extension shadows, non-unique indexes, and shape triggers."""

    _require_sqlite_column_alter()
    for name in _C5_REPLACED_UPDATE_NAMES:
        op.execute(f"DROP TRIGGER IF EXISTS {name}")
    for statement in _C5_HARDENED_UPDATE_SQL:
        op.execute(statement)
    for table, column, parent in _EXTENSION_COLUMNS:
        op.execute(
            sa.text(
                f"ALTER TABLE {table} ADD COLUMN {column} VARCHAR(36) "
                f"REFERENCES {parent}(id) {_FK_RESTRICT}"
            )
        )
        op.execute(sa.text(f"CREATE INDEX ix_{table}_{column} ON {table} ({column})"))
    for statement in _C6_TRIGGER_SQL:
        op.execute(statement)


def downgrade() -> None:
    """Remove only C6 shadows, leaving 0046 rows and C5 ownership shadows intact."""

    _require_sqlite_column_alter()
    for name in _C6_TRIGGER_NAMES:
        op.execute(f"DROP TRIGGER IF EXISTS {name}")
    for table, column, _parent in reversed(_EXTENSION_COLUMNS):
        op.execute(sa.text(f"DROP INDEX IF EXISTS ix_{table}_{column}"))
        op.execute(sa.text(f"ALTER TABLE {table} DROP COLUMN {column}"))
    for name in _C5_REPLACED_UPDATE_NAMES:
        op.execute(f"DROP TRIGGER IF EXISTS {name}")
    for statement in _C5_LEGACY_UPDATE_SQL:
        op.execute(statement)
