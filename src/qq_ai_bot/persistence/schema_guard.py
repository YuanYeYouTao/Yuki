"""Fail-closed startup validation for the frozen 3.8 canonical schema."""

from __future__ import annotations

from collections.abc import Mapping

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

CANONICAL_SCHEMA_REVISION = "0053"

_REQUIRED_COLUMNS: Mapping[str, frozenset[str]] = {
    "social_operation_receipts": frozenset(
        {
            "id",
            "source_turn_id",
            "tool_call_id",
            "payload_hash",
            "status",
            "target_id",
            "presence_id",
        }
    ),
    "memory_recall_receipts": frozenset(
        {
            "consumer",
            "attribution_status",
            "attribution_reason",
            "attribution_completed_at",
            "tool_read_success_count",
            "tool_read_empty_count",
            "tool_read_ambiguous_count",
            "tool_read_permission_denied_count",
            "tool_read_duplicate_count",
            "tool_read_infrastructure_failure_count",
        }
    ),
    "memory_recall_items": frozenset({"attribution_evaluated"}),
    "persons": frozenset({"id", "enabled", "revision"}),
    "identity_bindings": frozenset(
        {
            "id",
            "person_id",
            "platform",
            "external_account_id",
            "first_seen_at",
            "last_seen_at",
        }
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
    "presences": frozenset({"id", "platform", "external_account_id", "enabled"}),
    "canonical_conversations": frozenset({"id", "kind", "generation"}),
    "conversation_legacy_aliases": frozenset({"id", "conversation_id", "scope_key", "is_primary"}),
    "chat_events": frozenset(
        {
            "canonical_event_id",
            "canonical_conversation_id",
            "author_kind",
            "caused_by_event_id",
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
    """Validate the migration head and the canonical storage boundary once."""

    if not database_url.startswith("sqlite+aiosqlite:///"):
        raise CanonicalSchemaError("Yuki 3.8 supports only its canonical SQLite schema")

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
                    f"{CANONICAL_SCHEMA_REVISION}"
                )
            revision_rows = await connection.execute(
                text("SELECT version_num FROM alembic_version")
            )
            revisions = tuple(str(row[0]) for row in revision_rows)
            if revisions != (CANONICAL_SCHEMA_REVISION,):
                raise CanonicalSchemaError(
                    "database migration head is unsupported; expected canonical revision "
                    f"{CANONICAL_SCHEMA_REVISION}"
                )

            forbidden = sorted(tables & _FORBIDDEN_TABLES)
            if forbidden:
                raise CanonicalSchemaError("database still contains retired identity storage")

            for table, required in _REQUIRED_COLUMNS.items():
                if table not in tables:
                    raise CanonicalSchemaError("database canonical schema is incomplete")
                quoted_table = table.replace('"', '""')
                column_rows = await connection.execute(text(f'PRAGMA table_info("{quoted_table}")'))
                columns = {str(row[1]) for row in column_rows}
                if not required.issubset(columns):
                    raise CanonicalSchemaError("database canonical schema is incomplete")

            foreign_key_rows = await connection.execute(text("PRAGMA foreign_key_check"))
            if foreign_key_rows.first() is not None:
                raise CanonicalSchemaError("database canonical foreign-key integrity check failed")
    finally:
        await engine.dispose()
