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


_REQUIRED_COLUMNS: Mapping[str, frozenset[str]] = {
    "automations": frozenset(
        {"canonical_conversation_id", "source_event_id", "creation_source_key", "creation_max_runs"}
    ),
    "automation_versions": frozenset({"script_json", "script_hash", "authority_snapshot_json"}),
    "chat_history_fts": frozenset({"tokens"}),
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
            "original_event_id",
            "original_conversation_id",
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
    "client_devices": frozenset({"id", "person_id", "purpose", "token_hash", "revoked_at"}),
    "client_executors": frozenset({"id", "person_id", "token_hash", "revision", "revoked_at"}),
    "executor_grants": frozenset(
        {"executor_id", "capability", "scope_alias", "scope_digest", "revision"}
    ),
    "executor_operations": frozenset(
        {"op_id", "executor_id", "person_id", "work_id", "tool_call_key", "state", "receipt_hash"}
    ),
    "client_link_codes": frozenset(
        {"id", "person_id", "purpose", "code_hash", "expires_at", "used_at"}
    ),
    "external_agent_instances": frozenset({"id", "device_id", "local_id", "catalog_json"}),
    "external_agent_sessions": frozenset({"id", "instance_id", "native_id"}),
    "external_agent_executions": frozenset({"id", "session_id", "native_id"}),
    "external_agent_operations": frozenset(
        {
            "id",
            "instance_id",
            "session_id",
            "actor_person_id",
            "work_id",
            "source_json",
            "action",
            "payload_hash",
            "command_json",
            "run_id",
            "native_execution_id",
            "result_json",
            "created_at",
            "dispatched_at",
            "detached_at",
        }
    ),
    "external_agent_events": frozenset(
        {"id", "device_id", "event_id", "operation_id", "kind", "payload_json", "created_at"}
    ),
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
    """The configured database is not the bundled canonical schema."""


async def require_canonical_schema(database_url: str) -> None:
    """Validate the bundled version and metadata without scanning business data."""
    from yuki.persistence.database import _complete

    _, cancellation = await _complete(_check_canonical_schema(database_url))
    if cancellation is not None:
        raise cancellation


async def _check_canonical_schema(database_url: str) -> None:

    if not database_url.startswith("sqlite+aiosqlite:///"):
        raise CanonicalSchemaError("Yuki supports only its canonical SQLite schema")

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

            from yuki.persistence.history_search_index import DELETE_HISTORY_TRIGGER

            for name, expected in {
                "chat_history_fts_ad": DELETE_HISTORY_TRIGGER,
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
