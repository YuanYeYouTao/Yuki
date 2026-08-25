"""Frozen C5 ownership-shadow trigger SQL shared by ORM create_all.

Revision 0046 must embed identical literals and must not import this module.

Triggers encode UUID4 shape and cross-column ownership rules only. Parent
existence and parent DELETE / primary-key UPDATE are enforced by real
FOREIGN KEY constraints. relationship_events.actor_user_id is mutation
origin, not an owned subject, and therefore has no canonical Person shadow.
"""

from __future__ import annotations

from typing import TypedDict


class OwnershipColumnSpec(TypedDict):
    column: str
    parent_table: str
    parent_column: str


class OwnershipTableSpec(TypedDict):
    table: str
    created_by: str
    reason: str
    columns: tuple[OwnershipColumnSpec, ...]


C5_OWNERSHIP_INVENTORY: tuple[OwnershipTableSpec, ...] = (
    {
        "table": "people",
        "created_by": "0005 create_all",
        "reason": (
            "legacy person and person-setting carrier; Yuki/ignored bot rows stay "
            "NULL; one Person may own many people rows so the shadow is not UNIQUE"
        ),
        "columns": (
            {
                "column": "canonical_person_id",
                "parent_table": "persons",
                "parent_column": "id",
            },
        ),
    },
    {
        "table": "groups",
        "created_by": "0005 create_all",
        "reason": (
            "legacy group and group-setting carrier; one Space may own many "
            "groups rows so the shadow is not UNIQUE"
        ),
        "columns": (
            {
                "column": "canonical_space_id",
                "parent_table": "spaces",
                "parent_column": "id",
            },
        ),
    },
    {
        "table": "person_aliases",
        "created_by": "0005 create_all",
        "reason": ("alias is owned by a Person; nonempty group_scope is an optional Space"),
        "columns": (
            {
                "column": "canonical_person_id",
                "parent_table": "persons",
                "parent_column": "id",
            },
            {
                "column": "canonical_space_id",
                "parent_table": "spaces",
                "parent_column": "id",
            },
        ),
    },
    {
        "table": "memberships",
        "created_by": "0005 create_all",
        "reason": "membership is one Person known inside one Space",
        "columns": (
            {
                "column": "canonical_person_id",
                "parent_table": "persons",
                "parent_column": "id",
            },
            {
                "column": "canonical_space_id",
                "parent_table": "spaces",
                "parent_column": "id",
            },
        ),
    },
    {
        "table": "person_relationships",
        "created_by": "0007 explicit create_table",
        "reason": "affection/trust scores are owned by the target Person",
        "columns": (
            {
                "column": "canonical_person_id",
                "parent_table": "persons",
                "parent_column": "id",
            },
        ),
    },
    {
        "table": "relationship_events",
        "created_by": "0007 explicit create_table",
        "reason": "audit row is owned by the target Person whose scores changed",
        "columns": (
            {
                "column": "canonical_person_id",
                "parent_table": "persons",
                "parent_column": "id",
            },
        ),
    },
    {
        "table": "relationship_jobs",
        "created_by": "0007 explicit create_table",
        "reason": "evaluation job is owned by the target Person being scored",
        "columns": (
            {
                "column": "canonical_person_id",
                "parent_table": "persons",
                "parent_column": "id",
            },
        ),
    },
    {
        "table": "person_time_settings",
        "created_by": "0012 explicit create_table",
        "reason": "timezone preference is a person setting",
        "columns": (
            {
                "column": "canonical_person_id",
                "parent_table": "persons",
                "parent_column": "id",
            },
        ),
    },
    {
        "table": "person_speech_preferences",
        "created_by": "0017 explicit create_table",
        "reason": "voice mode preference is a person setting",
        "columns": (
            {
                "column": "canonical_person_id",
                "parent_table": "persons",
                "parent_column": "id",
            },
        ),
    },
    {
        "table": "memory_facts",
        "created_by": "0020 explicit create_table; visibility added by 0027",
        "reason": (
            "fact subject is Person and/or Space; SELF visibility is a separate "
            "Person/Space owner and SELF itself is not a Person"
        ),
        "columns": (
            {
                "column": "canonical_subject_person_id",
                "parent_table": "persons",
                "parent_column": "id",
            },
            {
                "column": "canonical_subject_space_id",
                "parent_table": "spaces",
                "parent_column": "id",
            },
            {
                "column": "canonical_visibility_person_id",
                "parent_table": "persons",
                "parent_column": "id",
            },
            {
                "column": "canonical_visibility_space_id",
                "parent_table": "spaces",
                "parent_column": "id",
            },
        ),
    },
)

C5_EXCLUDED_OWNERSHIP: tuple[tuple[str, str], ...] = (
    (
        "relationship_events.actor_user_id",
        "manual-mutation origin/audit; NULL on automatic events; not the owned subject",
    ),
    (
        "memory_jobs/evidence/reflection/dream/tool receipts",
        "C21 canonical memory ownership, not C5 person/space setting shadows",
    ),
    (
        "automation/plugin/config/emoji/MCP/observability",
        "C6 extension ownership shadows",
    ),
    (
        "chat_events/conversation_scopes",
        "C4 ledger/conversation shadows",
    ),
    (
        "speech_voice_profiles/references/generations",
        "C6 speech engine ledger, not person preference",
    ),
    (
        "admin_operation_events/agent_actions",
        "operator audit/observability, not person/space ownership",
    ),
)

C5_OWNERSHIP_TABLES: tuple[str, ...] = tuple(item["table"] for item in C5_OWNERSHIP_INVENTORY)
C5_OWNERSHIP_COLUMNS: dict[str, tuple[str, ...]] = {
    item["table"]: tuple(column["column"] for column in item["columns"])
    for item in C5_OWNERSHIP_INVENTORY
}
C5_OWNERSHIP_INDEXES: tuple[str, ...] = tuple(
    f"ix_{item['table']}_{column['column']}"
    for item in C5_OWNERSHIP_INVENTORY
    for column in item["columns"]
)
C5_OWNERSHIP_FOREIGN_KEYS: tuple[tuple[str, str, str, str], ...] = tuple(
    (item["table"], column["column"], column["parent_table"], column["parent_column"])
    for item in C5_OWNERSHIP_INVENTORY
    for column in item["columns"]
)

_UUID4_GLOB = (
    "[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]-"
    "[0-9a-f][0-9a-f][0-9a-f][0-9a-f]-4[0-9a-f][0-9a-f][0-9a-f]-"
    "[89ab][0-9a-f][0-9a-f][0-9a-f]-"
    "[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]"
    "[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]"
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


_C5_SHAPE_DISCRIMINATORS: dict[str, tuple[str, ...]] = {
    "person_aliases": ("group_scope",),
    "memory_facts": ("scope_type", "visibility_type"),
}


def _trigger_pair(
    table: str,
    columns: tuple[str, ...],
    valid_sql: str,
    message: str,
) -> tuple[str, str]:
    column_list = ", ".join(columns)
    insert_name = f"trg_{table}_ownership_shadow_insert"
    update_name = f"trg_{table}_ownership_shadow_update"
    insert_sql = f"""
CREATE TRIGGER {insert_name}
BEFORE INSERT ON {table}
BEGIN
    SELECT RAISE(ABORT, '{message}')
    WHERE NOT ({valid_sql});
END
""".strip()
    update_sql = f"""
CREATE TRIGGER {update_name}
BEFORE UPDATE OF {column_list} ON {table}
BEGIN
    SELECT RAISE(ABORT, '{message}')
    WHERE NOT ({valid_sql});
END
""".strip()
    return insert_sql, update_sql


def _c5_trigger_sql() -> tuple[str, ...]:
    statements: list[str] = []
    for item in C5_OWNERSHIP_INVENTORY:
        columns = tuple(column["column"] for column in item["columns"])
        table = item["table"]
        if table == "memory_facts":
            valid = _memory_facts_shadow_valid_sql()
            message = "invalid memory fact ownership shadow"
        elif table == "person_aliases":
            valid = _alias_shadow_valid_sql()
            message = "invalid person alias ownership shadow"
        else:
            valid = _uuid4_columns_valid_sql(columns)
            message = f"invalid {table} ownership shadow"
        watch = (*columns, *_C5_SHAPE_DISCRIMINATORS.get(table, ()))
        statements.extend(_trigger_pair(table, watch, valid, message))
    return tuple(statements)


C5_TRIGGER_SQL: tuple[str, ...] = _c5_trigger_sql()
C5_TRIGGER_NAMES: tuple[str, ...] = tuple(
    f"trg_{item['table']}_ownership_shadow_{action}"
    for item in C5_OWNERSHIP_INVENTORY
    for action in ("insert", "update")
)
