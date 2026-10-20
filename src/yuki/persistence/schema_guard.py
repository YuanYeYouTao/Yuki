"""Startup validation for the canonical schema shipped with this code."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from yuki.conversation.projection_chunks_schema import (
    PROJECTION_TRIGGERS_CURRENT,
)
from yuki.persistence.artifact_refs import ARTIFACT_REFERENCE_TRIGGERS


def canonical_schema_revision(root: Path | None = None) -> str:
    """Read the single head shipped with this code, never the database's own version."""
    heads = ScriptDirectory(str((root or Path.cwd()) / "migrations")).get_heads()
    if len(heads) != 1:
        raise CanonicalSchemaError(f"Expected one bundled migration head, got {heads!r}")
    return heads[0]


def _normalized_input_repair_index_sql(sql: str) -> str:
    """Preserve partial-index literals, including SQLite doubled-quote escapes."""
    result: list[str] = []
    position = 0
    literal = False
    while position < len(sql):
        character = sql[position]
        if character == "'":
            result.append(character)
            if literal and position + 1 < len(sql) and sql[position + 1] == "'":
                result.append("'")
                position += 2
                continue
            literal = not literal
        elif literal or (character != '"' and not character.isspace()):
            result.append(character)
        position += 1
    return "".join(result)


_REQUIRED_COLUMNS: Mapping[str, frozenset[str]] = {
    "chat_event_artifact_refs": frozenset({"event_id", "artifact_id", "version"}),
    "canonical_event_receipts": frozenset(
        {"ingress_account_id", "transport_namespace", "ingress_presence_id"}
    ),
    "ordinary_turn_admissions": frozenset(
        {
            "event_id",
            "conversation_id",
            "generation",
            "source_revision",
            "actor_person_id",
            "presence_id",
            "activation_id",
            "coordinator_version",
            "unit_key",
            "target_hint",
            "basis_json",
            "route",
            "work_id",
            "input_id",
            "created",
        }
    ),
    "model_context_observations": frozenset(
        {
            "id",
            "conversation_id",
            "generation",
            "actor_id",
            "read_scope",
            "source_key",
            "version",
            "payload_json",
            "parent_sources_json",
            "summary_view_key",
            "privacy_generation",
        }
    ),
    "prompt_projection_chunks": frozenset({"view_key", "epoch_id", "ordinal", "payload_json"}),
    "model_context_selections": frozenset(
        {
            "id",
            "view_key",
            "conversation_id",
            "generation",
            "source_key",
            "event_ids_json",
            "min_event_id",
            "max_event_id",
            "payload_json",
        }
    ),
    "tool_artifact_refs": frozenset({"owner_kind", "owner_id", "handle_id"}),
    "runtime_protocol_objects": frozenset({"sha256", "byte_size", "prepared_at", "deleting"}),
    "runtime_protocol_refs": frozenset({"work_id", "sha256"}),
    "runtime_protocol_usage": frozenset({"id", "byte_size"}),
    "tool_artifacts": frozenset(
        {"handle_id", "work_id", "effect_key", "deleting", "sha256", "access_json"}
    ),
    "runtime_work_recovery": frozenset(
        {"work_id", "activation_id", "exit_reason", "failure_json", "attempts", "not_before"}
    ),
    "runtime_delivery_intents": frozenset(
        {
            "id",
            "work_id",
            "kind",
            "state",
            "payload_json",
            "receipt_json",
            "message_count",
        }
    ),
    "runtime_automation_cursors": frozenset({"run_id", "script_hash", "phase", "payload_json"}),
    "runtime_subagents": frozenset(
        {
            "work_id",
            "brief_json",
            "result_json",
            "owner",
            "fence",
            "lease_until",
            "archived_at",
        }
    ),
    "runtime_work_budgets": frozenset({"root_id", "models", "tools", "model_limit", "tool_limit"}),
    "runtime_work_media": frozenset({"sha256", "content"}),
    "runtime_work_media_refs": frozenset({"work_id", "sha256"}),
    "runtime_work": frozenset(
        {
            "id",
            "parent_work_id",
            "conversation_id",
            "generation",
            "state",
            "revision",
            "model_requests",
            "tool_calls",
            "sent_messages",
            "active_seconds",
            "checkpoint_json",
        }
    ),
    "runtime_work_scopes": frozenset(
        {"conversation_id", "generation", "cancel_epoch", "fence", "owner", "lease_until"}
    ),
    "runtime_work_inputs": frozenset(
        {"id", "work_id", "state", "ready", "prepare_owner", "payload_json"}
    ),
    "runtime_work_effects": frozenset({"effect_key", "work_id", "state", "receipt_json"}),
    "runtime_work_journal": frozenset(
        {"archived_journal_json", "work_id", "contract", "source_revision", "phase", "payload_json"}
    ),
    "sandbox_task_runs": frozenset(
        {"request_id", "progress_json", "source_json", "completion_json"}
    ),
    "sandbox_task_continuations": frozenset(
        {"request_id", "state", "claim_token", "attempts", "reason", "outcome_json", "updated_at"}
    ),
    "prompt_projections": frozenset(
        {
            "view_key",
            "conversation_id",
            "generation",
            "source_revision",
            "starts_after_event_id",
            "epoch_id",
            "context_key",
            "revision",
            "rebuild_reason",
            "invalidated_reason",
            "tail_ordinal",
            "item_count",
            "byte_size",
            "updated_at",
            "selected_summary_text",
            "selected_summary_coverage",
        }
    ),
    "social_operation_receipts": frozenset(
        {
            "id",
            "event_id",
            "source_turn_id",
            "tool_call_id",
            "payload_hash",
            "planned_parts",
            "status",
            "target_id",
            "presence_id",
            "prepared_json",
        }
    ),
    "persons": frozenset({"id", "enabled", "revision", "display_name", "merged_into_person_id"}),
    "accounts": frozenset({"id", "platform", "external_account_id", "display_name"}),
    "account_assignments": frozenset(
        {"id", "account_id", "kind", "person_id", "status", "started_at", "ended_at"}
    ),
    "account_space_profiles": frozenset({"account_id", "space_binding_id", "display_name"}),
    "account_name_observations": frozenset(
        {"account_id", "assignment_id", "space_binding_id", "name"}
    ),
    "client_devices": frozenset({"id", "person_id", "token_hash", "revoked_at"}),
    "client_link_codes": frozenset({"id", "person_id", "code_hash", "expires_at", "used_at"}),
    "spaces": frozenset({"id", "enabled", "autonomous_enabled", "require_mention", "revision"}),
    "space_bindings": frozenset(
        {
            "id",
            "space_id",
            "platform",
            "external_space_id",
            "first_seen_at",
            "last_seen_at",
        }
    ),
    "canonical_conversations": frozenset(
        {"id", "kind", "identity_binding_id", "generation", "prompt_source_revision"}
    ),
    "conversation_legacy_aliases": frozenset({"id", "conversation_id", "scope_key", "is_primary"}),
    "chat_events": frozenset(
        {
            "canonical_event_id",
            "audio_transcript",
            "canonical_conversation_id",
            "author_kind",
            "caused_by_event_id",
            "reply_to_event_id",
        }
    ),
    "memory_facts": frozenset(
        {
            "canonical_subject_person_id",
            "canonical_subject_space_id",
            "canonical_visibility_person_id",
            "canonical_visibility_space_id",
        }
    ),
}

_FORBIDDEN_TABLES = frozenset(
    {
        "people",
        "groups",
        "conversation_scopes",
        "conversation_rollups",
        "conversation_rollup_jobs",
        "conversation_rollup_emergency_overlays",
        "canonical_conversation_rollups",
        "canonical_conversation_rollup_jobs",
        "canonical_conversation_rollup_emergency_overlays",
        "canonical_rollup_signals",
        "identity_runtime_state",
        "identity_backfill_runs",
        "identity_conflicts",
        "identity_cutover_manifests",
        "identity_cutover_runs",
    }
)


class CanonicalSchemaError(RuntimeError):
    """The configured database is not the supported 3.8 canonical schema."""


async def require_canonical_schema(database_url: str) -> None:
    """Validate the bundled version and metadata without scanning business data."""

    if not database_url.startswith("sqlite+aiosqlite:///"):
        raise CanonicalSchemaError("Yuki 3.8 supports only its canonical SQLite schema")

    expected_revision = canonical_schema_revision()
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            table_rows = await connection.execute(
                text("SELECT name FROM sqlite_master WHERE type = 'table'")
            )
            tables = {str(row[0]) for row in table_rows}
            if "alembic_version" not in tables:
                raise CanonicalSchemaError(
                    "database is not initialized; upgrade it to canonical revision "
                    f"{expected_revision}"
                )
            revision_rows = await connection.execute(
                text("SELECT version_num FROM alembic_version")
            )
            revisions = tuple(str(row[0]) for row in revision_rows)
            if revisions != (expected_revision,):
                raise CanonicalSchemaError(
                    "database migration head is unsupported; expected canonical revision "
                    f"{expected_revision}"
                )

            from yuki.runtime.effect_schema import INVOCATION_INDEXES
            from yuki.runtime.work_query_schema import query_index_sql

            for index_name, expected_sql in query_index_sql().items():
                row = (
                    await connection.execute(
                        text(
                            "SELECT tbl_name, sql FROM sqlite_master "
                            "WHERE type='index' AND name=:name"
                        ),
                        {"name": index_name},
                    )
                ).first()
                if (
                    row is None
                    or row[0] != "runtime_work"
                    or row[1] is None
                    or "".join(row[1].replace('"', "").split())
                    != "".join(expected_sql.replace('"', "").split())
                ):
                    raise CanonicalSchemaError("database Work query index is missing or changed")

            reference_sql = await connection.scalar(
                text("SELECT sql FROM sqlite_master WHERE type='index' AND name=:name"),
                {"name": "ix_runtime_effects_domain_ref"},
            )
            if reference_sql is None or _normalized_input_repair_index_sql(reference_sql) != (
                _normalized_input_repair_index_sql(
                    INVOCATION_INDEXES["ix_runtime_effects_domain_ref"].replace(
                        "IF NOT EXISTS ", ""
                    )
                )
            ):
                raise CanonicalSchemaError(
                    "database effect domain reference index is missing or changed"
                )

            forbidden = sorted(tables & _FORBIDDEN_TABLES)
            if forbidden:
                raise CanonicalSchemaError("database still contains retired identity storage")

            for table, required in _REQUIRED_COLUMNS.items():
                if table not in tables:
                    raise CanonicalSchemaError("database canonical schema is incomplete")
                quoted_table = table.replace('"', '""')
                column_rows = await connection.execute(text(f'PRAGMA table_info("{quoted_table}")'))
                column_info = list(column_rows)
                columns = {str(row[1]) for row in column_info}
                if not required.issubset(columns):
                    raise CanonicalSchemaError("database canonical schema is incomplete")
                if table == "runtime_work_budgets":
                    if any(
                        row[3] for row in column_info if row[1] in {"model_limit", "tool_limit"}
                    ):
                        raise CanonicalSchemaError("database budget limits must be nullable")

            index_name = "ix_memory_evidence_tool_receipt"
            index_rows = await connection.execute(text('PRAGMA index_list("memory_evidence")'))
            index = next((row for row in index_rows if row[1] == index_name), None)
            index_sql = await connection.scalar(
                text("SELECT sql FROM sqlite_master WHERE type='index' AND name=:name"),
                {"name": index_name},
            )
            index_columns = tuple(
                row[2]
                for row in await connection.execute(text(f'PRAGMA index_info("{index_name}")'))
            )
            if (
                index is None
                or index[2] != 0
                or index[4] != 1
                or index_columns != ("tool_receipt_id",)
                or " ".join(str(index_sql).lower().split()).split(" where ", 1)[-1]
                != "tool_receipt_id is not null"
            ):
                raise CanonicalSchemaError(
                    "database memory receipt reference index is missing or changed"
                )
            for index_name, table, index_columns, error_category in (
                (
                    "ix_plugin_background_turn_unbound_due",
                    "plugin_background_turn_jobs",
                    ("status", "work_id", "next_attempt_at"),
                    "plugin unbound job",
                ),
                (
                    "ix_plugin_background_turn_expired_lease",
                    "plugin_background_turn_jobs",
                    ("status", "lease_until", "next_attempt_at"),
                    "plugin expired lease",
                ),
                (
                    "ix_chat_event_artifact_refs_artifact_version",
                    "chat_event_artifact_refs",
                    ("artifact_id", "version", "event_id"),
                    "artifact source",
                ),
                (
                    "ix_memory_facts_status_valid_until_id",
                    "memory_facts",
                    ("status", "valid_until", "id"),
                    "memory expiration",
                ),
                (
                    "ix_memory_self_reflection_cycles_status_created",
                    "memory_self_reflection_cycles",
                    ("status", "created_at"),
                    "reflection cycle",
                ),
                (
                    "ix_memory_self_reflection_runs_retry_next",
                    "memory_self_reflection_runs",
                    ("retry_state", "next_attempt_at"),
                    "reflection retry",
                ),
                (
                    "ix_memory_self_reflection_cycles_delivery",
                    "memory_self_reflection_cycles",
                    ("trigger", "delivery_state", "status", "created_at"),
                    "reflection delivery",
                ),
                (
                    "ix_account_names_name_scope_account",
                    "account_name_observations",
                    ("name", "space_binding_id", "account_id"),
                    "mention name lookup",
                ),
                (
                    "ix_media_analyses_expires_at",
                    "media_analyses",
                    ("expires_at",),
                    "cache cleanup",
                ),
                (
                    "ix_web_search_runs_created_at",
                    "web_search_runs",
                    ("created_at",),
                    "cache cleanup",
                ),
                (
                    "ix_runtime_work_state_updated",
                    "runtime_work",
                    ("state", "updated"),
                    "cache cleanup",
                ),
                (
                    "ix_memory_dream_clusters_status_id",
                    "memory_dream_clusters",
                    ("status", "id"),
                    "maintenance",
                ),
                (
                    "ix_social_operation_scope_updated",
                    "social_operation_receipts",
                    ("source_conversation_id", "updated_at"),
                    "reply maintenance",
                ),
                (
                    "ix_context_selection_range",
                    "model_context_selections",
                    ("view_key", "generation", "max_event_id", "id"),
                    "projection provenance",
                ),
                (
                    "ix_protocol_objects_gc_cursor",
                    "runtime_protocol_objects",
                    ("deleting", "prepared_at", "sha256"),
                    "reply maintenance",
                ),
                (
                    "ix_evidence_compaction_items_fact_before_status",
                    "memory_evidence_compaction_items",
                    ("fact_id", "evidence_before", "status"),
                    "evidence compaction",
                ),
            ):
                retained = (
                    await connection.execute(
                        text(
                            "SELECT tbl_name, sql FROM sqlite_master "
                            "WHERE type='index' AND name=:name"
                        ),
                        {"name": index_name},
                    )
                ).first()
                indexes = await connection.execute(text(f'PRAGMA index_list("{table}")'))
                index = next((row for row in indexes if row[1] == index_name), None)
                index_keys = tuple(
                    (row[2], row[3], row[4])
                    for row in await connection.execute(text(f'PRAGMA index_xinfo("{index_name}")'))
                    if row[5] == 1
                )
                if (
                    retained is None
                    or retained[0] != table
                    or retained[1] is None
                    or index is None
                    or index[2] != 0
                    or index[3] != "c"
                    or index[4] != 0
                    or index_keys != tuple((column, 0, "BINARY") for column in index_columns)
                ):
                    raise CanonicalSchemaError(
                        f"database {error_category} index is missing or changed"
                    )
            from yuki.runtime.work_query_schema import (
                INPUT_REPAIR_INDEX_NAME,
                INPUT_REPAIR_INDEX_SQL,
            )

            repair_index = (
                await connection.execute(
                    text("SELECT type,tbl_name,sql FROM sqlite_master WHERE name=:name"),
                    {"name": INPUT_REPAIR_INDEX_NAME},
                )
            ).first()
            repair_shape = next(
                (
                    row
                    for row in await connection.execute(
                        text('PRAGMA index_list("runtime_work_inputs")')
                    )
                    if row[1] == INPUT_REPAIR_INDEX_NAME
                ),
                None,
            )
            repair_keys = tuple(
                (row[2], row[3], row[4])
                for row in await connection.execute(
                    text(f'PRAGMA index_xinfo("{INPUT_REPAIR_INDEX_NAME}")')
                )
                if row[5] == 1
            )
            if (
                repair_index is None
                or repair_index[0] != "index"
                or repair_index[1] != "runtime_work_inputs"
                or repair_index[2] is None
                or repair_shape is None
                or repair_shape[2] != 0
                or repair_shape[3] != "c"
                or repair_shape[4] != 1
                or repair_keys != (("id", 0, "BINARY"),)
                or _normalized_input_repair_index_sql(repair_index[2])
                != _normalized_input_repair_index_sql(INPUT_REPAIR_INDEX_SQL)
            ):
                raise CanonicalSchemaError("database abandoned input index is missing or changed")
            for index_name, table, index_columns, predicate, category in (
                (
                    "ix_chat_events_social_source_author",
                    "chat_events",
                    ("author_person_id", "id"),
                    "direction = 'inbound' AND author_kind = 'person'",
                    "social source",
                ),
                (
                    "ix_chat_events_conversation_message_sender_time",
                    "chat_events",
                    ("canonical_conversation_id", "sender_user_id", "occurred_at", "id"),
                    "suppression_status = 'keeper' AND event_kind = 'message'",
                    "mention activity",
                ),
                (
                    "ix_prompt_projections_active_updated",
                    "prompt_projections",
                    ("updated_at", "view_key", "byte_size"),
                    "invalidated_reason IS NULL",
                    "active projection",
                ),
                (
                    "ix_runtime_subagents_unarchived",
                    "runtime_subagents",
                    ("lease_until", "work_id"),
                    "archived_at IS NULL",
                    "subagent archive candidate",
                ),
            ):
                expected_sql = (
                    f"CREATE INDEX {index_name} ON {table} ({', '.join(index_columns)}) "
                    f"WHERE {predicate}"
                )
                found = (
                    await connection.execute(
                        text("SELECT type,tbl_name,sql FROM sqlite_master WHERE name=:name"),
                        {"name": index_name},
                    )
                ).first()
                shape = next(
                    (
                        row
                        for row in await connection.execute(text(f'PRAGMA index_list("{table}")'))
                        if row[1] == index_name
                    ),
                    None,
                )
                keys = tuple(
                    (row[2], row[3], row[4])
                    for row in await connection.execute(text(f'PRAGMA index_xinfo("{index_name}")'))
                    if row[5] == 1
                )
                if (
                    found is None
                    or found[0] != "index"
                    or found[1] != table
                    or found[2] is None
                    or shape is None
                    or shape[2] != 0
                    or shape[3] != "c"
                    or shape[4] != 1
                    or keys != tuple((column, 0, "BINARY") for column in index_columns)
                    or _normalized_input_repair_index_sql(found[2])
                    != _normalized_input_repair_index_sql(expected_sql)
                ):
                    raise CanonicalSchemaError(f"database {category} index is missing or changed")
            trigger_rows = await connection.execute(
                text("SELECT name, sql FROM sqlite_master WHERE type='trigger'")
            )
            triggers = {str(row[0]): str(row[1]) for row in trigger_rows}
            from yuki.runtime.protocol_schema import QUOTA_SQL

            protocol_triggers = {
                statement.split()[5]: statement
                for statement in QUOTA_SQL
                if statement.startswith("CREATE TRIGGER")
            }

            for name, expected in {
                **PROJECTION_TRIGGERS_CURRENT,
                **ARTIFACT_REFERENCE_TRIGGERS,
                **protocol_triggers,
                **_identity_triggers(),
            }.items():
                actual = triggers.get(name, "").replace("IF NOT EXISTS ", "")
                expected = expected.replace("IF NOT EXISTS ", "")
                if " ".join(actual.split()) != " ".join(expected.split()):
                    raise CanonicalSchemaError(
                        "database projection invalidation trigger is missing or changed"
                    )
    finally:
        await engine.dispose()


def _identity_triggers() -> dict[str, str]:
    from yuki.conversation.canonical_schema import ASSIGNMENT_ROLE_TRIGGER_SQL, C3_TRIGGER_SQL

    return {sql.split()[2]: sql for sql in (*C3_TRIGGER_SQL, *ASSIGNMENT_ROLE_TRIGGER_SQL)}
