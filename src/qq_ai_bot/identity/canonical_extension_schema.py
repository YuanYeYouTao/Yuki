"""Frozen C6 extension-shadow trigger SQL shared by ORM create_all.

Revision 0047 must embed identical literals and must not import this module.

Triggers encode UUID4 shape and mutually exclusive scope/target shapes.
They do not require v1 rows to populate shadows. Parent existence and
parent DELETE / primary-key UPDATE are enforced by real FOREIGN KEY
constraints. bot_user_id, provider, plugin, and automation identities
are never treated as Person owners.
"""

from __future__ import annotations

from typing import TypedDict


class ExtensionColumnSpec(TypedDict):
    column: str
    parent_table: str
    parent_column: str


class ExtensionTableSpec(TypedDict):
    table: str
    created_by: str
    reason: str
    columns: tuple[ExtensionColumnSpec, ...]


C6_EXTENSION_INVENTORY: tuple[ExtensionTableSpec, ...] = (
    {
        "table": "automations",
        "created_by": "0012 explicit create_table",
        "reason": (
            "creator is a Person; v2 routes by Person or Space; bot_user_id is a "
            "Presence correlation, not an owner"
        ),
        "columns": (
            {
                "column": "canonical_creator_person_id",
                "parent_table": "persons",
                "parent_column": "id",
            },
            {
                "column": "canonical_target_person_id",
                "parent_table": "persons",
                "parent_column": "id",
            },
            {
                "column": "canonical_target_space_id",
                "parent_table": "spaces",
                "parent_column": "id",
            },
            {
                "column": "canonical_presence_id",
                "parent_table": "presences",
                "parent_column": "id",
            },
        ),
    },
    {
        "table": "plugin_config_values",
        "created_by": "0013 explicit create_table",
        "reason": "plugin config scope is global, a Person, or a Space",
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
        "table": "plugin_state",
        "created_by": "0013 explicit create_table",
        "reason": "optional subject_user_id is a Person; plugin-global KV stays NULL",
        "columns": (
            {
                "column": "canonical_person_id",
                "parent_table": "persons",
                "parent_column": "id",
            },
        ),
    },
    {
        "table": "plugin_agent_sessions",
        "created_by": "0013 explicit create_table",
        "reason": (
            "owner_user_id is a Person; group scope is a Space; plugin scope is "
            "plugin-owned and has no Person/Space owner"
        ),
        "columns": (
            {
                "column": "canonical_owner_person_id",
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
        "table": "plugin_agent_messages",
        "created_by": "0013 explicit create_table",
        "reason": "sender_user_id is the message author Person, not the plugin",
        "columns": (
            {
                "column": "canonical_sender_person_id",
                "parent_table": "persons",
                "parent_column": "id",
            },
        ),
    },
    {
        "table": "plugin_background_target_grants",
        "created_by": "0028 explicit create_table",
        "reason": (
            "private target is a Person, group target is a Space; created_by is a "
            "Person; bot_user_id is Presence correlation, not an owner"
        ),
        "columns": (
            {
                "column": "canonical_target_person_id",
                "parent_table": "persons",
                "parent_column": "id",
            },
            {
                "column": "canonical_target_space_id",
                "parent_table": "spaces",
                "parent_column": "id",
            },
            {
                "column": "canonical_created_by_person_id",
                "parent_table": "persons",
                "parent_column": "id",
            },
            {
                "column": "canonical_presence_id",
                "parent_table": "presences",
                "parent_column": "id",
            },
        ),
    },
    {
        "table": "plugin_notification_outbox",
        "created_by": "0028 explicit create_table",
        "reason": (
            "delivery target is a Person or Space; conversation is correlation; "
            "bot_user_id is Presence correlation, not an owner"
        ),
        "columns": (
            {
                "column": "canonical_target_person_id",
                "parent_table": "persons",
                "parent_column": "id",
            },
            {
                "column": "canonical_target_space_id",
                "parent_table": "spaces",
                "parent_column": "id",
            },
            {
                "column": "canonical_conversation_id",
                "parent_table": "canonical_conversations",
                "parent_column": "id",
            },
            {
                "column": "canonical_presence_id",
                "parent_table": "presences",
                "parent_column": "id",
            },
        ),
    },
    {
        "table": "plugin_background_turn_jobs",
        "created_by": "0028 explicit create_table",
        "reason": (
            "background turn target is a Person or Space; conversation is "
            "correlation; bot_user_id is Presence correlation, not an owner"
        ),
        "columns": (
            {
                "column": "canonical_target_person_id",
                "parent_table": "persons",
                "parent_column": "id",
            },
            {
                "column": "canonical_target_space_id",
                "parent_table": "spaces",
                "parent_column": "id",
            },
            {
                "column": "canonical_conversation_id",
                "parent_table": "canonical_conversations",
                "parent_column": "id",
            },
            {
                "column": "canonical_presence_id",
                "parent_table": "presences",
                "parent_column": "id",
            },
        ),
    },
    {
        "table": "runtime_config_overrides",
        "created_by": "0008 explicit create_table",
        "reason": "runtime config scope is global, a Person, or a Space",
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
        "table": "emoji_assets",
        "created_by": "0014 explicit create_table",
        "reason": (
            "first_seen_user_id/group_id are discovery provenance Person/Space, "
            "not exclusive owners of the global asset"
        ),
        "columns": (
            {
                "column": "canonical_first_seen_person_id",
                "parent_table": "persons",
                "parent_column": "id",
            },
            {
                "column": "canonical_first_seen_space_id",
                "parent_table": "spaces",
                "parent_column": "id",
            },
        ),
    },
    {
        "table": "emoji_scope_states",
        "created_by": "0014 explicit create_table",
        "reason": "emoji enablement is global or one Space",
        "columns": (
            {
                "column": "canonical_space_id",
                "parent_table": "spaces",
                "parent_column": "id",
            },
        ),
    },
    {
        "table": "emoji_usage_events",
        "created_by": "0014 explicit create_table",
        "reason": "usage actor is a Person; group_id is the Space where it was used",
        "columns": (
            {
                "column": "canonical_actor_person_id",
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
        "table": "speech_generations",
        "created_by": "0015 explicit create_table",
        "reason": "generation is correlated to a Conversation, not owned by a provider",
        "columns": (
            {
                "column": "canonical_conversation_id",
                "parent_table": "canonical_conversations",
                "parent_column": "id",
            },
        ),
    },
    {
        "table": "tool_invocations",
        "created_by": "0019 explicit create_table",
        "reason": "MCP/tool audit correlates to a Conversation; provider_id is not an owner",
        "columns": (
            {
                "column": "canonical_conversation_id",
                "parent_table": "canonical_conversations",
                "parent_column": "id",
            },
        ),
    },
    {
        "table": "web_search_runs",
        "created_by": "0006 explicit create_table",
        "reason": (
            "web tool ledger correlates to a Conversation via conversation_key; "
            "provider is not an owner"
        ),
        "columns": (
            {
                "column": "canonical_conversation_id",
                "parent_table": "canonical_conversations",
                "parent_column": "id",
            },
        ),
    },
    {
        "table": "model_invocations",
        "created_by": "0018 explicit create_table",
        "reason": "model telemetry may correlate to a Conversation when a turn is bound",
        "columns": (
            {
                "column": "canonical_conversation_id",
                "parent_table": "canonical_conversations",
                "parent_column": "id",
            },
        ),
    },
    {
        "table": "runtime_turn_observations",
        "created_by": "0037 explicit create_table",
        "reason": (
            "turn observation correlates to a Conversation; private scope is a "
            "Person and group scope is a Space"
        ),
        "columns": (
            {
                "column": "canonical_conversation_id",
                "parent_table": "canonical_conversations",
                "parent_column": "id",
            },
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
        "table": "reply_effect_events",
        "created_by": "0039 explicit create_table",
        "reason": "cadence row correlates to the Conversation that produced the reply",
        "columns": (
            {
                "column": "canonical_conversation_id",
                "parent_table": "canonical_conversations",
                "parent_column": "id",
            },
        ),
    },
)

C6_EXCLUDED_EXTENSION: tuple[tuple[str, str], ...] = (
    (
        "plugin_installations",
        "global plugin catalog; a plugin is not a Person/Space/Conversation",
    ),
    (
        "plugin_audit_events.actor_user_id",
        "operator origin/audit, not an owned subject; later control-plane audit",
    ),
    (
        "plugin_media_artifacts",
        "plugin blob catalog with no person/space/conversation key",
    ),
    (
        "automation_versions",
        "child of automations; updated_by is mutation origin, not the owner",
    ),
    (
        "automation_runs/automation_step_runs",
        "child of automations; no independent person/space/conversation key",
    ),
    (
        "speech_voice_profiles/references",
        "provider engine catalog; provider is not an owner",
    ),
    (
        "person_speech_preferences",
        "C5 person setting shadow already exists",
    ),
    (
        "emoji_jobs",
        "child of emoji_assets; no independent owner key",
    ),
    (
        "emoji_descriptions",
        "QQ emoji identity cache; no person/space/conversation owner",
    ),
    (
        "mcp_server_states/mcp_tool_cache/tool_artifacts",
        "MCP/provider catalog and blob handles; provider_id is not an owner",
    ),
    (
        "agent_actions/admin_operation_events",
        "operator audit; Control Plane/C9+, not extension ownership",
    ),
    (
        "web_search_sources",
        "child of web_search_runs via run_id; conversation correlation lives on the run",
    ),
    (
        "media_analyses",
        "vision cache; not C6 extension ownership",
    ),
    (
        "people/groups/aliases/memberships/relationships/time/memory_facts",
        "C5 person/space setting shadows",
    ),
    (
        "chat_events/conversation_scopes/canonical_event_receipts",
        "C4 ledger/conversation shadows",
    ),
    (
        "memory jobs/evidence/reflection/dream/tool receipts",
        "C21 canonical memory ownership",
    ),
    (
        "gateway_connections/presence_active_routes/delivery_routes/yuki tables",
        "forbidden new tables; Gateway/Ingress/Cutover are later commits",
    ),
)

C6_OWNERSHIP_TABLES: tuple[str, ...] = tuple(item["table"] for item in C6_EXTENSION_INVENTORY)
C6_OWNERSHIP_COLUMNS: dict[str, tuple[str, ...]] = {
    item["table"]: tuple(column["column"] for column in item["columns"])
    for item in C6_EXTENSION_INVENTORY
}
C6_OWNERSHIP_INDEXES: tuple[str, ...] = tuple(
    f"ix_{item['table']}_{column['column']}"
    for item in C6_EXTENSION_INVENTORY
    for column in item["columns"]
)
C6_OWNERSHIP_FOREIGN_KEYS: tuple[tuple[str, str, str, str], ...] = tuple(
    (item["table"], column["column"], column["parent_table"], column["parent_column"])
    for item in C6_EXTENSION_INVENTORY
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


C6_SHAPE_DISCRIMINATORS: dict[str, tuple[str, ...]] = {
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
    statements: list[str] = []
    for item in C6_EXTENSION_INVENTORY:
        table = item["table"]
        columns = tuple(column["column"] for column in item["columns"])
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
            valid = _uuid4_columns_valid_sql(columns)
        watch = (*columns, *C6_SHAPE_DISCRIMINATORS.get(table, ()))
        statements.extend(_trigger_pair(table, watch, valid, f"invalid {table} extension shadow"))
    return tuple(statements)


C6_TRIGGER_SQL: tuple[str, ...] = _c6_trigger_sql()
C6_TRIGGER_NAMES: tuple[str, ...] = tuple(
    f"trg_{item['table']}_extension_shadow_{action}"
    for item in C6_EXTENSION_INVENTORY
    for action in ("insert", "update")
)
