"""Create canonical identity foundation tables.

Revision ID: 0043
Revises: 0042
Create Date: 2026-08-24

This revision is frozen and self-contained. It must not import application
modules or create tables from current ORM metadata.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0043"
down_revision: str | None = "0042"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_UUID4_GLOB = (
    "[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]-"
    "[0-9a-f][0-9a-f][0-9a-f][0-9a-f]-4[0-9a-f][0-9a-f][0-9a-f]-"
    "[89ab][0-9a-f][0-9a-f][0-9a-f]-"
    "[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]"
    "[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]"
)


def _uuid4_sql(column: str) -> str:
    return f"length({column}) = 36 AND {column} = lower({column}) AND {column} GLOB '{_UUID4_GLOB}'"


def _optional_uuid4_sql(column: str) -> str:
    return f"{column} IS NULL OR ({_uuid4_sql(column)})"


def _platform_sql(column: str) -> str:
    return (
        f"length({column}) > 0 AND length({column}) <= 32 "
        f"AND {column} = lower({column}) AND {column} = trim({column})"
    )


def _external_id_sql(column: str) -> str:
    return f"length({column}) > 0 AND length({column}) <= 255 AND {column} = trim({column})"


def upgrade() -> None:
    """Create the eight foundation tables and seed the v1 runtime singleton."""

    op.create_table(
        "persons",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(_uuid4_sql("id"), name="ck_persons_id"),
        sa.CheckConstraint("enabled IN (0, 1)", name="ck_persons_enabled"),
        sa.CheckConstraint("revision >= 1", name="ck_persons_revision"),
    )
    op.create_table(
        "spaces",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("autonomous_enabled", sa.Boolean(), nullable=False),
        sa.Column("require_mention", sa.Boolean(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(_uuid4_sql("id"), name="ck_spaces_id"),
        sa.CheckConstraint("enabled IN (0, 1)", name="ck_spaces_enabled"),
        sa.CheckConstraint("autonomous_enabled IN (0, 1)", name="ck_spaces_autonomous_enabled"),
        sa.CheckConstraint("require_mention IN (0, 1)", name="ck_spaces_require_mention"),
        sa.CheckConstraint("revision >= 1", name="ck_spaces_revision"),
    )
    op.create_table(
        "presences",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("platform", sa.String(32), nullable=False),
        sa.Column("external_account_id", sa.String(255), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("ingest_eligible", sa.Boolean(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "platform",
            "external_account_id",
            name="uq_presences_platform_account",
        ),
        sa.CheckConstraint(_uuid4_sql("id"), name="ck_presences_id"),
        sa.CheckConstraint(_platform_sql("platform"), name="ck_presences_platform"),
        sa.CheckConstraint(
            _external_id_sql("external_account_id"),
            name="ck_presences_external_account_id",
        ),
        sa.CheckConstraint("enabled IN (0, 1)", name="ck_presences_enabled"),
        sa.CheckConstraint("ingest_eligible IN (0, 1)", name="ck_presences_ingest_eligible"),
        sa.CheckConstraint("revision >= 1", name="ck_presences_revision"),
    )
    op.create_table(
        "identity_bindings",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("person_id", sa.String(36), nullable=False),
        sa.Column("platform", sa.String(32), nullable=False),
        sa.Column("external_account_id", sa.String(255), nullable=False),
        sa.Column("display_name", sa.String(128), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["person_id"], ["persons.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint(
            "platform",
            "external_account_id",
            name="uq_identity_bindings_platform_account",
        ),
        sa.CheckConstraint(_uuid4_sql("id"), name="ck_identity_bindings_id"),
        sa.CheckConstraint(_uuid4_sql("person_id"), name="ck_identity_bindings_person_id"),
        sa.CheckConstraint(_platform_sql("platform"), name="ck_identity_bindings_platform"),
        sa.CheckConstraint(
            _external_id_sql("external_account_id"),
            name="ck_identity_bindings_external_account_id",
        ),
        sa.CheckConstraint("status IN ('active', 'disabled')", name="ck_identity_bindings_status"),
        sa.CheckConstraint("revision >= 1", name="ck_identity_bindings_revision"),
    )
    op.create_index("ix_identity_bindings_person_id", "identity_bindings", ["person_id"])
    op.create_table(
        "space_bindings",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("space_id", sa.String(36), nullable=False),
        sa.Column("platform", sa.String(32), nullable=False),
        sa.Column("external_space_id", sa.String(255), nullable=False),
        sa.Column("display_name", sa.String(128), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["space_id"], ["spaces.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint(
            "platform",
            "external_space_id",
            name="uq_space_bindings_platform_space",
        ),
        sa.CheckConstraint(_uuid4_sql("id"), name="ck_space_bindings_id"),
        sa.CheckConstraint(_uuid4_sql("space_id"), name="ck_space_bindings_space_id"),
        sa.CheckConstraint(_platform_sql("platform"), name="ck_space_bindings_platform"),
        sa.CheckConstraint(
            _external_id_sql("external_space_id"),
            name="ck_space_bindings_external_space_id",
        ),
        sa.CheckConstraint("status IN ('active', 'disabled')", name="ck_space_bindings_status"),
        sa.CheckConstraint("revision >= 1", name="ck_space_bindings_revision"),
    )
    op.create_index("ix_space_bindings_space_id", "space_bindings", ["space_id"])
    op.create_table(
        "identity_runtime_state",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(8), nullable=False),
        sa.Column("cutover_id", sa.String(36), nullable=True),
        sa.Column("source_fingerprint", sa.String(64), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("id = 1", name="ck_identity_runtime_state_singleton"),
        sa.CheckConstraint("state IN ('v1', 'v2')", name="ck_identity_runtime_state_state"),
        sa.CheckConstraint(
            _optional_uuid4_sql("cutover_id"),
            name="ck_identity_runtime_state_cutover_id",
        ),
        sa.CheckConstraint(
            "source_fingerprint IS NULL OR length(source_fingerprint) > 0",
            name="ck_identity_runtime_state_source_fingerprint",
        ),
        sa.CheckConstraint("revision >= 1", name="ck_identity_runtime_state_revision"),
        sa.CheckConstraint(
            "("
            "state = 'v1' AND cutover_id IS NULL "
            "AND source_fingerprint IS NULL AND completed_at IS NULL"
            ") OR ("
            "state = 'v2' AND cutover_id IS NOT NULL "
            "AND source_fingerprint IS NOT NULL AND completed_at IS NOT NULL"
            ")",
            name="ck_identity_runtime_state_epoch",
        ),
    )
    op.execute(
        """
        INSERT INTO identity_runtime_state (
            id, state, cutover_id, source_fingerprint, completed_at,
            revision, created_at, updated_at
        ) VALUES (
            1, 'v1', NULL, NULL, NULL, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
        )
        """
    )
    op.create_table(
        "identity_backfill_runs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("mode", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("checkpoint", sa.String(128), nullable=True),
        sa.Column("processed_count", sa.Integer(), nullable=False),
        sa.Column("persons_count", sa.Integer(), nullable=False),
        sa.Column("identity_bindings_count", sa.Integer(), nullable=False),
        sa.Column("spaces_count", sa.Integer(), nullable=False),
        sa.Column("space_bindings_count", sa.Integer(), nullable=False),
        sa.Column("presences_count", sa.Integer(), nullable=False),
        sa.Column("conflicts_count", sa.Integer(), nullable=False),
        sa.Column("skipped_count", sa.Integer(), nullable=False),
        sa.Column("error_category", sa.String(64), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("mode IN ('dry_run', 'apply')", name="ck_identity_backfill_runs_mode"),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'succeeded', 'failed', 'cancelled')",
            name="ck_identity_backfill_runs_status",
        ),
        sa.CheckConstraint(
            "processed_count >= 0 AND persons_count >= 0 "
            "AND identity_bindings_count >= 0 AND spaces_count >= 0 "
            "AND space_bindings_count >= 0 AND presences_count >= 0 "
            "AND conflicts_count >= 0 AND skipped_count >= 0",
            name="ck_identity_backfill_runs_counts",
        ),
        sa.CheckConstraint(
            "checkpoint IS NULL OR length(checkpoint) > 0",
            name="ck_identity_backfill_runs_checkpoint",
        ),
        sa.CheckConstraint(
            "error_category IS NULL OR length(error_category) > 0",
            name="ck_identity_backfill_runs_error_category",
        ),
        sa.CheckConstraint(
            "("
            "status = 'pending' AND started_at IS NULL AND finished_at IS NULL "
            "AND error_category IS NULL"
            ") OR ("
            "status = 'running' AND started_at IS NOT NULL AND finished_at IS NULL"
            ") OR ("
            "status IN ('succeeded', 'cancelled') AND started_at IS NOT NULL "
            "AND finished_at IS NOT NULL AND error_category IS NULL"
            ") OR ("
            "status = 'failed' AND started_at IS NOT NULL "
            "AND finished_at IS NOT NULL AND error_category IS NOT NULL"
            ")",
            name="ck_identity_backfill_runs_lifecycle",
        ),
    )
    op.create_index(
        "ix_identity_backfill_runs_status_created",
        "identity_backfill_runs",
        ["status", "created_at"],
    )
    op.create_table(
        "identity_conflicts",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("platform", sa.String(32), nullable=False),
        sa.Column("external_id", sa.String(255), nullable=False),
        sa.Column("subject_kind", sa.String(16), nullable=False),
        sa.Column("conflict_kind", sa.String(32), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("error_category", sa.String(64), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "platform",
            "external_id",
            "subject_kind",
            "conflict_kind",
            name="uq_identity_conflicts_subject",
        ),
        sa.CheckConstraint(_platform_sql("platform"), name="ck_identity_conflicts_platform"),
        sa.CheckConstraint(
            _external_id_sql("external_id"), name="ck_identity_conflicts_external_id"
        ),
        sa.CheckConstraint(
            "subject_kind IN ('account', 'space')",
            name="ck_identity_conflicts_subject_kind",
        ),
        sa.CheckConstraint(
            "conflict_kind IN ('ambiguous_identity', 'unclassified')",
            name="ck_identity_conflicts_kind",
        ),
        sa.CheckConstraint("status IN ('open', 'resolved')", name="ck_identity_conflicts_status"),
        sa.CheckConstraint(
            "("
            "status = 'open' AND resolved_at IS NULL"
            ") OR ("
            "status = 'resolved' AND resolved_at IS NOT NULL"
            ")",
            name="ck_identity_conflicts_lifecycle",
        ),
        sa.CheckConstraint(
            "error_category IS NULL OR length(error_category) > 0",
            name="ck_identity_conflicts_error_category",
        ),
    )
    op.create_index("ix_identity_conflicts_status", "identity_conflicts", ["status"])


def downgrade() -> None:
    """Drop only the eight foundation tables and return to 0042."""

    op.drop_index("ix_identity_conflicts_status", table_name="identity_conflicts")
    op.drop_table("identity_conflicts")
    op.drop_index(
        "ix_identity_backfill_runs_status_created",
        table_name="identity_backfill_runs",
    )
    op.drop_table("identity_backfill_runs")
    op.drop_table("identity_runtime_state")
    op.drop_index("ix_identity_bindings_person_id", table_name="identity_bindings")
    op.drop_table("identity_bindings")
    op.drop_index("ix_space_bindings_space_id", table_name="space_bindings")
    op.drop_table("space_bindings")
    op.drop_table("presences")
    op.drop_table("persons")
    op.drop_table("spaces")
