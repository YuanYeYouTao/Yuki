"""Create canonical conversations, aliases, routes, and command receipts.

Revision ID: 0044
Revises: 0043
Create Date: 2026-08-24

This revision is frozen and self-contained. It must not import application
modules or create tables from current ORM metadata.

The two cyclic conversation/alias tables are created directly in this
transaction with foreign keys ON. SQLite permits the first CREATE TABLE to
reference the not-yet-created second table when the FK is DEFERRABLE
INITIALLY DEFERRED. Cyclic actions use NO ACTION so a populated downgrade
can drop both tables after their triggers in the same transaction.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0044"
down_revision: str | None = "0043"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_UUID4_GLOB = (
    "[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]-"
    "[0-9a-f][0-9a-f][0-9a-f][0-9a-f]-4[0-9a-f][0-9a-f][0-9a-f]-"
    "[89ab][0-9a-f][0-9a-f][0-9a-f]-"
    "[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]"
    "[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]"
)
_SHA256_GLOB = "[0-9a-f]" * 64

_C3_TRIGGER_SQL: tuple[str, ...] = (
    """
CREATE TRIGGER trg_canonical_conversations_primary_alias_immutable
BEFORE UPDATE OF primary_alias_id, primary_marker ON canonical_conversations
BEGIN
    SELECT RAISE(ABORT, 'canonical conversation primary alias pointer is immutable')
    WHERE NEW.primary_alias_id IS NOT OLD.primary_alias_id
       OR NEW.primary_marker IS NOT OLD.primary_marker;
END
""".strip(),
    """
CREATE TRIGGER trg_conversation_legacy_aliases_primary_update
BEFORE UPDATE ON conversation_legacy_aliases
BEGIN
    SELECT RAISE(ABORT, 'pinned primary alias cannot be changed')
    WHERE EXISTS (
        SELECT 1 FROM canonical_conversations
        WHERE id = OLD.conversation_id AND primary_alias_id = OLD.id
    )
    AND (
        NEW.id IS NOT OLD.id
        OR NEW.conversation_id IS NOT OLD.conversation_id
        OR NEW.is_primary IS NOT OLD.is_primary
        OR NEW.scope_key IS NOT OLD.scope_key
    );
END
""".strip(),
    """
CREATE TRIGGER trg_conversation_legacy_aliases_primary_delete
BEFORE DELETE ON conversation_legacy_aliases
BEGIN
    SELECT RAISE(ABORT, 'pinned primary alias cannot be deleted')
    WHERE EXISTS (
        SELECT 1 FROM canonical_conversations
        WHERE id = OLD.conversation_id AND primary_alias_id = OLD.id
    );
END
""".strip(),
    """
CREATE TRIGGER trg_person_active_routes_consistency_insert
BEFORE INSERT ON person_active_routes
BEGIN
    SELECT RAISE(ABORT, 'person active route ownership or platform mismatch')
    WHERE NOT EXISTS (
        SELECT 1
        FROM identity_bindings AS binding
        JOIN presences AS presence ON presence.id = NEW.presence_id
        WHERE binding.id = NEW.identity_binding_id
          AND binding.person_id = NEW.person_id
          AND binding.platform = presence.platform
    );
END
""".strip(),
    """
CREATE TRIGGER trg_person_active_routes_consistency_update
BEFORE UPDATE ON person_active_routes
BEGIN
    SELECT RAISE(ABORT, 'person active route ownership or platform mismatch')
    WHERE NOT EXISTS (
        SELECT 1
        FROM identity_bindings AS binding
        JOIN presences AS presence ON presence.id = NEW.presence_id
        WHERE binding.id = NEW.identity_binding_id
          AND binding.person_id = NEW.person_id
          AND binding.platform = presence.platform
    );
END
""".strip(),
    """
CREATE TRIGGER trg_space_binding_ingest_routes_consistency_insert
BEFORE INSERT ON space_binding_ingest_routes
BEGIN
    SELECT RAISE(ABORT, 'space binding ingest route platform mismatch')
    WHERE NOT EXISTS (
        SELECT 1
        FROM space_bindings AS binding
        JOIN presences AS presence ON presence.id = NEW.ingest_presence_id
        WHERE binding.id = NEW.space_binding_id
          AND binding.platform = presence.platform
    );
END
""".strip(),
    """
CREATE TRIGGER trg_space_binding_ingest_routes_consistency_update
BEFORE UPDATE ON space_binding_ingest_routes
BEGIN
    SELECT RAISE(ABORT, 'space binding ingest route platform mismatch')
    WHERE NOT EXISTS (
        SELECT 1
        FROM space_bindings AS binding
        JOIN presences AS presence ON presence.id = NEW.ingest_presence_id
        WHERE binding.id = NEW.space_binding_id
          AND binding.platform = presence.platform
    );
END
""".strip(),
    """
CREATE TRIGGER trg_space_active_routes_consistency_insert
BEFORE INSERT ON space_active_routes
BEGIN
    SELECT RAISE(ABORT, 'space active route ownership or platform mismatch')
    WHERE NOT EXISTS (
        SELECT 1
        FROM space_bindings AS binding
        JOIN presences AS presence ON presence.id = NEW.presence_id
        WHERE binding.id = NEW.space_binding_id
          AND binding.space_id = NEW.space_id
          AND binding.platform = presence.platform
    );
END
""".strip(),
    """
CREATE TRIGGER trg_space_active_routes_consistency_update
BEFORE UPDATE ON space_active_routes
BEGIN
    SELECT RAISE(ABORT, 'space active route ownership or platform mismatch')
    WHERE NOT EXISTS (
        SELECT 1
        FROM space_bindings AS binding
        JOIN presences AS presence ON presence.id = NEW.presence_id
        WHERE binding.id = NEW.space_binding_id
          AND binding.space_id = NEW.space_id
          AND binding.platform = presence.platform
    );
END
""".strip(),
    """
CREATE TRIGGER trg_identity_bindings_route_consistency_update
BEFORE UPDATE OF person_id, platform ON identity_bindings
BEGIN
    SELECT RAISE(ABORT, 'identity binding update would break person active route')
    WHERE EXISTS (
        SELECT 1
        FROM person_active_routes AS route
        JOIN presences AS presence ON presence.id = route.presence_id
        WHERE route.identity_binding_id = NEW.id
          AND (
            route.person_id IS NOT NEW.person_id
            OR NEW.platform IS NOT presence.platform
          )
    );
END
""".strip(),
    """
CREATE TRIGGER trg_space_bindings_route_consistency_update
BEFORE UPDATE OF space_id, platform ON space_bindings
BEGIN
    SELECT RAISE(ABORT, 'space binding update would break space routes')
    WHERE EXISTS (
        SELECT 1
        FROM space_binding_ingest_routes AS route
        JOIN presences AS presence ON presence.id = route.ingest_presence_id
        WHERE route.space_binding_id = NEW.id
          AND NEW.platform IS NOT presence.platform
    )
    OR EXISTS (
        SELECT 1
        FROM space_active_routes AS route
        JOIN presences AS presence ON presence.id = route.presence_id
        WHERE route.space_binding_id = NEW.id
          AND (
            route.space_id IS NOT NEW.space_id
            OR NEW.platform IS NOT presence.platform
          )
    );
END
""".strip(),
    """
CREATE TRIGGER trg_presences_route_consistency_update
BEFORE UPDATE OF platform ON presences
BEGIN
    SELECT RAISE(ABORT, 'presence platform update would break routes')
    WHERE EXISTS (
        SELECT 1
        FROM person_active_routes AS route
        JOIN identity_bindings AS binding ON binding.id = route.identity_binding_id
        WHERE route.presence_id = NEW.id
          AND binding.platform IS NOT NEW.platform
    )
    OR EXISTS (
        SELECT 1
        FROM space_binding_ingest_routes AS route
        JOIN space_bindings AS binding ON binding.id = route.space_binding_id
        WHERE route.ingest_presence_id = NEW.id
          AND binding.platform IS NOT NEW.platform
    )
    OR EXISTS (
        SELECT 1
        FROM space_active_routes AS route
        JOIN space_bindings AS binding ON binding.id = route.space_binding_id
        WHERE route.presence_id = NEW.id
          AND binding.platform IS NOT NEW.platform
    );
END
""".strip(),
)
_C3_TRIGGER_NAMES: tuple[str, ...] = (
    "trg_canonical_conversations_primary_alias_immutable",
    "trg_conversation_legacy_aliases_primary_update",
    "trg_conversation_legacy_aliases_primary_delete",
    "trg_person_active_routes_consistency_insert",
    "trg_person_active_routes_consistency_update",
    "trg_space_binding_ingest_routes_consistency_insert",
    "trg_space_binding_ingest_routes_consistency_update",
    "trg_space_active_routes_consistency_insert",
    "trg_space_active_routes_consistency_update",
    "trg_identity_bindings_route_consistency_update",
    "trg_space_bindings_route_consistency_update",
    "trg_presences_route_consistency_update",
)


def _uuid4_sql(column: str) -> str:
    return f"length({column}) = 36 AND {column} = lower({column}) AND {column} GLOB '{_UUID4_GLOB}'"


def _optional_uuid4_sql(column: str) -> str:
    return f"{column} IS NULL OR ({_uuid4_sql(column)})"


def _sha256_hex_sql(column: str) -> str:
    return (
        f"length({column}) = 64 AND {column} = lower({column}) AND {column} GLOB '{_SHA256_GLOB}'"
    )


def _trimmed_token_sql(column: str, max_length: int) -> str:
    return (
        f"length({column}) > 0 AND length({column}) <= {max_length} AND {column} = trim({column})"
    )


def upgrade() -> None:
    """Create the six C3 tables, then install frozen triggers."""

    op.create_table(
        "canonical_conversations",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("person_id", sa.String(36), nullable=True),
        sa.Column("space_id", sa.String(36), nullable=True),
        sa.Column("primary_alias_id", sa.String(36), nullable=False),
        sa.Column("primary_marker", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("starts_after_event_id", sa.Integer(), nullable=False),
        sa.Column("last_event_id", sa.Integer(), nullable=False),
        sa.Column("last_generation_change_event_id", sa.Integer(), nullable=False),
        sa.Column("covered_through_event_id", sa.Integer(), nullable=False),
        sa.Column("uncovered_event_count", sa.Integer(), nullable=False),
        sa.Column("uncovered_character_count", sa.Integer(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["person_id"],
            ["persons.id"],
            name="fk_canonical_conversations_person",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["space_id"],
            ["spaces.id"],
            name="fk_canonical_conversations_space",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["id", "primary_alias_id", "primary_marker"],
            [
                "conversation_legacy_aliases.conversation_id",
                "conversation_legacy_aliases.id",
                "conversation_legacy_aliases.is_primary",
            ],
            name="fk_canonical_conversations_primary_alias",
            deferrable=True,
            initially="DEFERRED",
            onupdate="NO ACTION",
            ondelete="NO ACTION",
        ),
        sa.CheckConstraint(_uuid4_sql("id"), name="ck_canonical_conversations_id"),
        sa.CheckConstraint("kind IN ('private', 'space')", name="ck_canonical_conversations_kind"),
        sa.CheckConstraint(
            "("
            "kind = 'private' AND person_id IS NOT NULL AND space_id IS NULL"
            ") OR ("
            "kind = 'space' AND space_id IS NOT NULL AND person_id IS NULL"
            ")",
            name="ck_canonical_conversations_owner_xor",
        ),
        sa.CheckConstraint(
            _optional_uuid4_sql("person_id"),
            name="ck_canonical_conversations_person_id",
        ),
        sa.CheckConstraint(
            _optional_uuid4_sql("space_id"),
            name="ck_canonical_conversations_space_id",
        ),
        sa.CheckConstraint(
            _uuid4_sql("primary_alias_id"),
            name="ck_canonical_conversations_primary_alias_id",
        ),
        sa.CheckConstraint("primary_marker = 1", name="ck_canonical_conversations_primary_marker"),
        sa.CheckConstraint("generation >= 1", name="ck_canonical_conversations_generation"),
        sa.CheckConstraint("revision >= 1", name="ck_canonical_conversations_revision"),
        sa.CheckConstraint(
            "starts_after_event_id >= 0 AND last_event_id >= 0 "
            "AND last_generation_change_event_id >= 0 AND covered_through_event_id >= 0 "
            "AND uncovered_event_count >= 0 AND uncovered_character_count >= 0 "
            "AND starts_after_event_id <= last_event_id "
            "AND last_generation_change_event_id <= last_event_id "
            "AND covered_through_event_id <= last_event_id "
            "AND starts_after_event_id <= covered_through_event_id",
            name="ck_canonical_conversations_watermarks",
        ),
    )
    op.create_index(
        "uq_canonical_conversations_person",
        "canonical_conversations",
        ["person_id"],
        unique=True,
        sqlite_where=sa.text("kind = 'private'"),
    )
    op.create_index(
        "uq_canonical_conversations_space",
        "canonical_conversations",
        ["space_id"],
        unique=True,
        sqlite_where=sa.text("kind = 'space'"),
    )
    op.create_table(
        "conversation_legacy_aliases",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("conversation_id", sa.String(36), nullable=False),
        sa.Column("scope_key", sa.String(255), nullable=False),
        sa.Column("is_primary", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["canonical_conversations.id"],
            name="fk_conversation_legacy_aliases_conversation",
            deferrable=True,
            initially="DEFERRED",
            onupdate="NO ACTION",
            ondelete="NO ACTION",
        ),
        sa.UniqueConstraint(
            "conversation_id",
            "id",
            "is_primary",
            name="uq_conversation_legacy_aliases_primary_target",
        ),
        sa.UniqueConstraint("scope_key", name="uq_conversation_legacy_aliases_scope_key"),
        sa.CheckConstraint(_uuid4_sql("id"), name="ck_conversation_legacy_aliases_id"),
        sa.CheckConstraint(
            _uuid4_sql("conversation_id"),
            name="ck_conversation_legacy_aliases_conversation_id",
        ),
        sa.CheckConstraint(
            _trimmed_token_sql("scope_key", 255),
            name="ck_conversation_legacy_aliases_scope_key",
        ),
        sa.CheckConstraint(
            "is_primary IN (0, 1)",
            name="ck_conversation_legacy_aliases_is_primary",
        ),
    )
    op.create_index(
        "ix_conversation_legacy_aliases_conversation_id",
        "conversation_legacy_aliases",
        ["conversation_id"],
    )
    op.create_index(
        "uq_conversation_legacy_aliases_primary",
        "conversation_legacy_aliases",
        ["conversation_id"],
        unique=True,
        sqlite_where=sa.text("is_primary = 1"),
    )
    op.create_table(
        "person_active_routes",
        sa.Column("person_id", sa.String(36), nullable=False),
        sa.Column("identity_binding_id", sa.String(36), nullable=False),
        sa.Column("presence_id", sa.String(36), nullable=False),
        sa.Column("route_generation", sa.Integer(), nullable=False),
        sa.Column("paused", sa.Boolean(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("person_id"),
        sa.ForeignKeyConstraint(
            ["person_id"],
            ["persons.id"],
            name="fk_person_active_routes_person",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["identity_binding_id"],
            ["identity_bindings.id"],
            name="fk_person_active_routes_binding",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["presence_id"],
            ["presences.id"],
            name="fk_person_active_routes_presence",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(_uuid4_sql("person_id"), name="ck_person_active_routes_person_id"),
        sa.CheckConstraint(
            _uuid4_sql("identity_binding_id"),
            name="ck_person_active_routes_binding_id",
        ),
        sa.CheckConstraint(_uuid4_sql("presence_id"), name="ck_person_active_routes_presence_id"),
        sa.CheckConstraint("route_generation >= 1", name="ck_person_active_routes_generation"),
        sa.CheckConstraint("paused IN (0, 1)", name="ck_person_active_routes_paused"),
        sa.CheckConstraint("revision >= 1", name="ck_person_active_routes_revision"),
    )
    op.create_table(
        "space_binding_ingest_routes",
        sa.Column("space_binding_id", sa.String(36), nullable=False),
        sa.Column("ingest_presence_id", sa.String(36), nullable=False),
        sa.Column("route_generation", sa.Integer(), nullable=False),
        sa.Column("paused", sa.Boolean(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("space_binding_id"),
        sa.ForeignKeyConstraint(
            ["space_binding_id"],
            ["space_bindings.id"],
            name="fk_space_binding_ingest_routes_binding",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["ingest_presence_id"],
            ["presences.id"],
            name="fk_space_binding_ingest_routes_presence",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            _uuid4_sql("space_binding_id"),
            name="ck_space_binding_ingest_routes_binding_id",
        ),
        sa.CheckConstraint(
            _uuid4_sql("ingest_presence_id"),
            name="ck_space_binding_ingest_routes_presence_id",
        ),
        sa.CheckConstraint(
            "route_generation >= 1", name="ck_space_binding_ingest_routes_generation"
        ),
        sa.CheckConstraint("paused IN (0, 1)", name="ck_space_binding_ingest_routes_paused"),
        sa.CheckConstraint("revision >= 1", name="ck_space_binding_ingest_routes_revision"),
    )
    op.create_table(
        "space_active_routes",
        sa.Column("space_id", sa.String(36), nullable=False),
        sa.Column("space_binding_id", sa.String(36), nullable=False),
        sa.Column("presence_id", sa.String(36), nullable=False),
        sa.Column("route_generation", sa.Integer(), nullable=False),
        sa.Column("paused", sa.Boolean(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("space_id"),
        sa.ForeignKeyConstraint(
            ["space_id"],
            ["spaces.id"],
            name="fk_space_active_routes_space",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["space_binding_id"],
            ["space_bindings.id"],
            name="fk_space_active_routes_binding",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["presence_id"],
            ["presences.id"],
            name="fk_space_active_routes_presence",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(_uuid4_sql("space_id"), name="ck_space_active_routes_space_id"),
        sa.CheckConstraint(
            _uuid4_sql("space_binding_id"),
            name="ck_space_active_routes_binding_id",
        ),
        sa.CheckConstraint(_uuid4_sql("presence_id"), name="ck_space_active_routes_presence_id"),
        sa.CheckConstraint("route_generation >= 1", name="ck_space_active_routes_generation"),
        sa.CheckConstraint("paused IN (0, 1)", name="ck_space_active_routes_paused"),
        sa.CheckConstraint("revision >= 1", name="ck_space_active_routes_revision"),
    )
    op.create_table(
        "control_command_receipts",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("principal_id", sa.String(36), nullable=False),
        sa.Column("request_id", sa.String(36), nullable=False),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("result_resource_id", sa.String(255), nullable=True),
        sa.Column("effective_state_json", sa.String(4096), nullable=True),
        sa.Column("result_revision", sa.Integer(), nullable=True),
        sa.Column("problem_code", sa.String(64), nullable=True),
        sa.Column("audit_id", sa.Integer(), nullable=True),
        sa.Column("operation_kind", sa.String(64), nullable=True),
        sa.Column("operation_ref", sa.String(128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "principal_id",
            "request_id",
            name="uq_control_command_receipts_principal_request",
        ),
        sa.ForeignKeyConstraint(
            ["audit_id"],
            ["admin_operation_events.id"],
            name="fk_control_command_receipts_audit",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            _uuid4_sql("principal_id"),
            name="ck_control_command_receipts_principal_id",
        ),
        sa.CheckConstraint(
            _uuid4_sql("request_id"),
            name="ck_control_command_receipts_request_id",
        ),
        sa.CheckConstraint(
            _sha256_hex_sql("payload_hash"),
            name="ck_control_command_receipts_payload_hash",
        ),
        sa.CheckConstraint(
            "status IN ('succeeded', 'failed')",
            name="ck_control_command_receipts_status",
        ),
        sa.CheckConstraint(
            "("
            "status = 'succeeded' AND problem_code IS NULL "
            "AND audit_id IS NOT NULL AND audit_id >= 1 "
            "AND effective_state_json IS NOT NULL"
            ") OR ("
            "status = 'failed' AND problem_code IS NOT NULL "
            "AND length(problem_code) > 0 AND length(problem_code) <= 64 "
            "AND problem_code = trim(problem_code) "
            "AND effective_state_json IS NULL "
            "AND result_resource_id IS NULL AND result_revision IS NULL"
            ")",
            name="ck_control_command_receipts_lifecycle",
        ),
        sa.CheckConstraint(
            "result_resource_id IS NULL OR (" + _trimmed_token_sql("result_resource_id", 255) + ")",
            name="ck_control_command_receipts_result_resource_id",
        ),
        sa.CheckConstraint(
            "result_revision IS NULL OR result_revision >= 1",
            name="ck_control_command_receipts_result_revision",
        ),
        sa.CheckConstraint(
            "audit_id IS NULL OR audit_id >= 1",
            name="ck_control_command_receipts_audit_id",
        ),
        sa.CheckConstraint(
            "effective_state_json IS NULL OR ("
            "length(effective_state_json) > 0 AND length(effective_state_json) <= 4096 "
            "AND effective_state_json = trim(effective_state_json) "
            "AND json_valid(effective_state_json) "
            "AND substr(effective_state_json, 1, 1) = '{'"
            ")",
            name="ck_control_command_receipts_effective_state",
        ),
        sa.CheckConstraint(
            "("
            "operation_kind IS NULL AND operation_ref IS NULL"
            ") OR ("
            "operation_kind IS NOT NULL AND operation_ref IS NOT NULL "
            f"AND {_trimmed_token_sql('operation_kind', 64)} "
            f"AND {_trimmed_token_sql('operation_ref', 128)}"
            ")",
            name="ck_control_command_receipts_operation",
        ),
    )
    op.create_index(
        "ix_control_command_receipts_principal_created",
        "control_command_receipts",
        ["principal_id", "created_at"],
    )
    for statement in _C3_TRIGGER_SQL:
        op.execute(statement)


def downgrade() -> None:
    """Drop C3 triggers first, then the six tables, returning to 0043."""

    for name in _C3_TRIGGER_NAMES:
        op.execute(f"DROP TRIGGER IF EXISTS {name}")
    op.drop_index(
        "ix_control_command_receipts_principal_created",
        table_name="control_command_receipts",
    )
    op.drop_table("control_command_receipts")
    op.drop_table("space_active_routes")
    op.drop_table("space_binding_ingest_routes")
    op.drop_table("person_active_routes")
    op.drop_index(
        "uq_conversation_legacy_aliases_primary",
        table_name="conversation_legacy_aliases",
    )
    op.drop_index(
        "ix_conversation_legacy_aliases_conversation_id",
        table_name="conversation_legacy_aliases",
    )
    op.drop_table("conversation_legacy_aliases")
    op.drop_index("uq_canonical_conversations_space", table_name="canonical_conversations")
    op.drop_index("uq_canonical_conversations_person", table_name="canonical_conversations")
    op.drop_table("canonical_conversations")
