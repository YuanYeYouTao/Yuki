"""Frozen C21 Memory-owner trigger SQL and index literals shared by ORM create_all.

Revision 0047 must embed identical literals and must not import this module.

Memory partitions are an independent family: person:{Person UUID} or
space:{Space UUID}. Conversation UUID, ConversationScope bot:* keys, and
Presence QQ are never Memory owners. memory_facts already has C5 subject/
visibility shadows; this module adds canonical active partial unique
indexes only. Job/receipt/reflection rows use XOR Person/Space owners.
Dream clusters store the same four fact-shaped owner columns so partition
identity does not include bot_user_id or external QQ.
"""

from __future__ import annotations

from typing import TypedDict


class MemoryOwnerColumnSpec(TypedDict):
    column: str
    parent_table: str
    parent_column: str


class MemoryOwnerTableSpec(TypedDict):
    table: str
    created_by: str
    reason: str
    columns: tuple[MemoryOwnerColumnSpec, ...]


C21_XOR_OWNER_INVENTORY: tuple[MemoryOwnerTableSpec, ...] = (
    {
        "table": "memory_jobs",
        "created_by": "0020 explicit create_table",
        "reason": (
            "live extraction batch is owned by one Person or one Space; "
            "conversation_key is provenance only"
        ),
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
        "table": "memory_tool_receipts",
        "created_by": "0027 explicit create_table",
        "reason": (
            "tool receipt cursor is owned by one Person or one Space; "
            "bot_user_id and conversation_key_hash are provenance only"
        ),
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
        "table": "memory_self_reflection_states",
        "created_by": "0027 explicit create_table",
        "reason": (
            "reflection cursor is owned by one Person or one Space; "
            "hash and bot_user_id are provenance only"
        ),
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
        "table": "memory_self_reflection_runs",
        "created_by": "0027 explicit create_table",
        "reason": (
            "reflection run is owned by one Person or one Space; "
            "hash and bot_user_id are provenance only"
        ),
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
)

C21_DREAM_CLUSTER_INVENTORY: MemoryOwnerTableSpec = {
    "table": "memory_dream_clusters",
    "created_by": "0033 explicit create_table",
    "reason": (
        "dream partition unit mirrors fact canonical subject/visibility plus kind; "
        "runs are global and are not partition owners; bot_user_id is provenance"
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
}

C21_EXCLUDED_MEMORY: tuple[tuple[str, str], ...] = (
    (
        "memory_facts owner columns",
        "C5 already added the four canonical subject/visibility shadows",
    ),
    (
        "memory_dream_runs",
        "global run ledger, not a partition owner; clusters carry fact-shaped owners",
    ),
    (
        "memory_evidence/mutation/rebuild/embedding/activation/recall",
        "child or rebuild artifacts; live partition identity lives on facts/jobs",
    ),
    (
        "memory_self_reflection_runtime/results",
        "singleton scan cursor and child result rows; owners live on state/run",
    ),
)

C21_XOR_OWNER_TABLES: tuple[str, ...] = tuple(item["table"] for item in C21_XOR_OWNER_INVENTORY)
C21_OWNER_TABLES: tuple[str, ...] = (*C21_XOR_OWNER_TABLES, C21_DREAM_CLUSTER_INVENTORY["table"])
C21_OWNERSHIP_COLUMNS: dict[str, tuple[str, ...]] = {
    **{
        item["table"]: tuple(column["column"] for column in item["columns"])
        for item in C21_XOR_OWNER_INVENTORY
    },
    C21_DREAM_CLUSTER_INVENTORY["table"]: tuple(
        column["column"] for column in C21_DREAM_CLUSTER_INVENTORY["columns"]
    ),
}
C21_OWNERSHIP_INDEXES: tuple[str, ...] = tuple(
    f"ix_{table}_{column}" for table, columns in C21_OWNERSHIP_COLUMNS.items() for column in columns
)
C21_OWNERSHIP_FOREIGN_KEYS: tuple[tuple[str, str, str, str], ...] = (
    *(
        (item["table"], column["column"], column["parent_table"], column["parent_column"])
        for item in C21_XOR_OWNER_INVENTORY
        for column in item["columns"]
    ),
    *(
        (
            C21_DREAM_CLUSTER_INVENTORY["table"],
            column["column"],
            column["parent_table"],
            column["parent_column"],
        )
        for column in C21_DREAM_CLUSTER_INVENTORY["columns"]
    ),
)

C21_FACT_UNIQUE_INDEX_SQL: tuple[tuple[str, str], ...] = (
    (
        "uq_memory_facts_active_canonical_person_key",
        "CREATE UNIQUE INDEX uq_memory_facts_active_canonical_person_key "
        "ON memory_facts (canonical_subject_person_id, kind, memory_key) "
        "WHERE status = 'active' AND scope_type = 'person' "
        "AND canonical_subject_person_id IS NOT NULL "
        "AND canonical_subject_space_id IS NULL",
    ),
    (
        "uq_memory_facts_active_canonical_person_group_key",
        "CREATE UNIQUE INDEX uq_memory_facts_active_canonical_person_group_key "
        "ON memory_facts (canonical_subject_person_id, canonical_subject_space_id, "
        "kind, memory_key) "
        "WHERE status = 'active' AND scope_type = 'person_group' "
        "AND canonical_subject_person_id IS NOT NULL "
        "AND canonical_subject_space_id IS NOT NULL",
    ),
    (
        "uq_memory_facts_active_canonical_group_key",
        "CREATE UNIQUE INDEX uq_memory_facts_active_canonical_group_key "
        "ON memory_facts (canonical_subject_space_id, kind, memory_key) "
        "WHERE status = 'active' AND scope_type = 'group' "
        "AND canonical_subject_space_id IS NOT NULL "
        "AND canonical_subject_person_id IS NULL",
    ),
    (
        "uq_memory_facts_active_canonical_self_key",
        "CREATE UNIQUE INDEX uq_memory_facts_active_canonical_self_key "
        "ON memory_facts ("
        "memory_key, visibility_type, "
        "COALESCE(canonical_visibility_person_id, ''), "
        "COALESCE(canonical_visibility_space_id, '')"
        ") WHERE status = 'active' AND scope_type = 'self'",
    ),
)
C21_FACT_UNIQUE_INDEX_NAMES: tuple[str, ...] = tuple(
    name for name, _sql in C21_FACT_UNIQUE_INDEX_SQL
)

C21_REFLECTION_UNIQUE_INDEX_SQL: tuple[tuple[str, str], ...] = (
    (
        "uq_memory_self_reflection_states_canonical_person",
        "CREATE UNIQUE INDEX uq_memory_self_reflection_states_canonical_person "
        "ON memory_self_reflection_states (canonical_person_id) "
        "WHERE canonical_person_id IS NOT NULL AND canonical_space_id IS NULL",
    ),
    (
        "uq_memory_self_reflection_states_canonical_space",
        "CREATE UNIQUE INDEX uq_memory_self_reflection_states_canonical_space "
        "ON memory_self_reflection_states (canonical_space_id) "
        "WHERE canonical_space_id IS NOT NULL AND canonical_person_id IS NULL",
    ),
    (
        "uq_memory_self_reflection_runs_canonical_person_slot",
        "CREATE UNIQUE INDEX uq_memory_self_reflection_runs_canonical_person_slot "
        "ON memory_self_reflection_runs (canonical_person_id, scheduled_slot) "
        "WHERE canonical_person_id IS NOT NULL AND canonical_space_id IS NULL",
    ),
    (
        "uq_memory_self_reflection_runs_canonical_space_slot",
        "CREATE UNIQUE INDEX uq_memory_self_reflection_runs_canonical_space_slot "
        "ON memory_self_reflection_runs (canonical_space_id, scheduled_slot) "
        "WHERE canonical_space_id IS NOT NULL AND canonical_person_id IS NULL",
    ),
)
C21_REFLECTION_UNIQUE_INDEX_NAMES: tuple[str, ...] = tuple(
    name for name, _sql in C21_REFLECTION_UNIQUE_INDEX_SQL
)

C21_MEMORY_FACT_CONFLICT_SQL: tuple[tuple[str, str], ...] = (
    (
        "canonical_person_fact",
        "SELECT 1 FROM memory_facts "
        "WHERE status = 'active' AND scope_type = 'person' "
        "AND canonical_subject_person_id IS NOT NULL "
        "GROUP BY canonical_subject_person_id, kind, memory_key "
        "HAVING COUNT(*) > 1 LIMIT 1",
    ),
    (
        "canonical_person_group_fact",
        "SELECT 1 FROM memory_facts "
        "WHERE status = 'active' AND scope_type = 'person_group' "
        "AND canonical_subject_person_id IS NOT NULL "
        "AND canonical_subject_space_id IS NOT NULL "
        "GROUP BY canonical_subject_person_id, canonical_subject_space_id, kind, memory_key "
        "HAVING COUNT(*) > 1 LIMIT 1",
    ),
    (
        "canonical_group_fact",
        "SELECT 1 FROM memory_facts "
        "WHERE status = 'active' AND scope_type = 'group' "
        "AND canonical_subject_space_id IS NOT NULL "
        "GROUP BY canonical_subject_space_id, kind, memory_key "
        "HAVING COUNT(*) > 1 LIMIT 1",
    ),
    (
        "canonical_self_fact",
        "SELECT 1 FROM memory_facts "
        "WHERE status = 'active' AND scope_type = 'self' "
        "GROUP BY memory_key, visibility_type, "
        "COALESCE(canonical_visibility_person_id, ''), "
        "COALESCE(canonical_visibility_space_id, '') "
        "HAVING COUNT(*) > 1 LIMIT 1",
    ),
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


def _not_both_sql(left: str, right: str) -> str:
    return f"NOT (NEW.{left} IS NOT NULL AND NEW.{right} IS NOT NULL)"


def _xor_owner_valid_sql() -> str:
    return (
        f"{_uuid4_columns_valid_sql(('canonical_person_id', 'canonical_space_id'))} AND "
        f"{_not_both_sql('canonical_person_id', 'canonical_space_id')}"
    )


def _dream_cluster_owner_valid_sql() -> str:
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
        } AND "
        f"{_not_both_sql('canonical_visibility_person_id', 'canonical_visibility_space_id')} AND "
        "NOT ("
        "(NEW.canonical_subject_person_id IS NOT NULL OR "
        "NEW.canonical_subject_space_id IS NOT NULL) AND "
        "(NEW.canonical_visibility_person_id IS NOT NULL OR "
        "NEW.canonical_visibility_space_id IS NOT NULL)"
        ")"
    )


def _c21_trigger_pair(
    table: str,
    columns: tuple[str, ...],
    valid_sql: str,
    message: str,
) -> tuple[str, str]:
    column_list = ", ".join(columns)
    insert_sql = f"""
CREATE TRIGGER trg_{table}_memory_owner_insert
BEFORE INSERT ON {table}
BEGIN
    SELECT RAISE(ABORT, '{message}')
    WHERE NOT ({valid_sql});
END
""".strip()
    update_sql = f"""
CREATE TRIGGER trg_{table}_memory_owner_update
BEFORE UPDATE OF {column_list} ON {table}
BEGIN
    SELECT RAISE(ABORT, '{message}')
    WHERE NOT ({valid_sql});
END
""".strip()
    return insert_sql, update_sql


def _c21_trigger_sql() -> tuple[str, ...]:
    statements: list[str] = []
    for item in C21_XOR_OWNER_INVENTORY:
        columns = tuple(column["column"] for column in item["columns"])
        statements.extend(
            _c21_trigger_pair(
                item["table"],
                columns,
                _xor_owner_valid_sql(),
                f"invalid {item['table']} memory owner",
            )
        )
    dream_columns = tuple(column["column"] for column in C21_DREAM_CLUSTER_INVENTORY["columns"])
    statements.extend(
        _c21_trigger_pair(
            C21_DREAM_CLUSTER_INVENTORY["table"],
            dream_columns,
            _dream_cluster_owner_valid_sql(),
            "invalid memory_dream_clusters memory owner",
        )
    )
    return tuple(statements)


C21_TRIGGER_SQL: tuple[str, ...] = _c21_trigger_sql()
C21_TRIGGER_NAMES: tuple[str, ...] = tuple(
    f"trg_{table}_memory_owner_{action}"
    for table in C21_OWNER_TABLES
    for action in ("insert", "update")
)


def memory_fact_canonical_conflict_kind(connection: object) -> str | None:
    """Return the first explicit canonical fact unique conflict, or None."""

    for kind, sql in C21_MEMORY_FACT_CONFLICT_SQL:
        row = connection.execute(sql).fetchone()  # type: ignore[attr-defined]
        if row is not None:
            return kind
    return None
