"""Persist exclusive participation ownership and idempotent host admission.

No controller is enabled or existing autonomous/chat execution altered by this migration.
"""

import sqlalchemy as sa
from alembic import op

revision = "0065"
down_revision = "0064"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Frozen DDL: future ORM changes must not alter this published migration.
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    if "autonomy_bindings" not in existing:
        op.create_table(
            "autonomy_bindings",
            sa.Column("conversation_id", sa.String(36), nullable=False),
            sa.Column("generation", sa.Integer(), nullable=False),
            sa.Column("master_enabled", sa.Boolean(), nullable=False),
            sa.Column("external_enabled", sa.Boolean(), nullable=False),
            sa.Column("effective_owner", sa.String(16), nullable=False),
            sa.Column("controller_epoch", sa.Integer(), nullable=False),
            sa.Column("revision", sa.Integer(), nullable=False),
            sa.Column("fallback_reason", sa.String(128), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("conversation_id", "generation"),
            sa.ForeignKeyConstraint(
                ["conversation_id"], ["canonical_conversations.id"], ondelete="RESTRICT"
            ),
            sa.CheckConstraint("generation >= 1 AND controller_epoch >= 0 AND revision >= 1"),
            sa.CheckConstraint("effective_owner IN ('off', 'legacy', 'semantic')"),
            sa.CheckConstraint(
                "(master_enabled = 0 AND effective_owner = 'off') OR "
                "(master_enabled = 1 AND effective_owner IN ('legacy', 'semantic'))"
            ),
            sa.CheckConstraint("effective_owner != 'semantic' OR external_enabled = 1"),
        )
    if "autonomy_initiative_runs" not in existing:
        op.create_table(
            "autonomy_initiative_runs",
            sa.Column("id", sa.String(36), nullable=False),
            sa.Column("proposal_id", sa.String(128), nullable=False),
            sa.Column("conversation_id", sa.String(36), nullable=False),
            sa.Column("generation", sa.Integer(), nullable=False),
            sa.Column("owner", sa.String(16), nullable=False),
            sa.Column("controller_epoch", sa.Integer(), nullable=False),
            sa.Column("space_id", sa.String(36), nullable=False),
            sa.Column("presence_id", sa.String(36), nullable=False),
            sa.Column("target_person_id", sa.String(36), nullable=True),
            sa.Column("payload_hash", sa.String(64), nullable=False),
            sa.Column("sources_json", sa.Text(), nullable=False),
            sa.Column("support_refs_json", sa.Text(), nullable=False),
            sa.Column("state", sa.String(16), nullable=False),
            sa.Column("feedback_sequence", sa.Integer(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.ForeignKeyConstraint(
                ["conversation_id", "generation"],
                ["autonomy_bindings.conversation_id", "autonomy_bindings.generation"],
                ondelete="RESTRICT",
            ),
            sa.ForeignKeyConstraint(["space_id"], ["spaces.id"], ondelete="RESTRICT"),
            sa.ForeignKeyConstraint(["presence_id"], ["presences.id"], ondelete="RESTRICT"),
            sa.ForeignKeyConstraint(["target_person_id"], ["persons.id"], ondelete="RESTRICT"),
            sa.UniqueConstraint(
                "conversation_id",
                "generation",
                "owner",
                "controller_epoch",
                "proposal_id",
                name="uq_autonomy_proposal_run",
            ),
            sa.CheckConstraint(
                "generation >= 1 AND controller_epoch >= 0 AND feedback_sequence >= 0"
            ),
            sa.CheckConstraint("owner IN ('legacy', 'semantic')"),
            sa.CheckConstraint(
                "state IN ('accepted', 'running', 'completed', 'no_reply', 'interrupted', 'failed')"
            ),
        )
        op.create_index(
            "ix_autonomy_runs_active",
            "autonomy_initiative_runs",
            ["conversation_id", "generation", "state"],
        )
        op.create_index(
            "uq_autonomy_runs_one_active",
            "autonomy_initiative_runs",
            ["conversation_id", "generation"],
            unique=True,
            sqlite_where=sa.text("state IN ('accepted', 'running')"),
        )
    if "autonomy_source_claims" not in existing:
        op.create_table(
            "autonomy_source_claims",
            sa.Column("conversation_id", sa.String(36), nullable=False),
            sa.Column("generation", sa.Integer(), nullable=False),
            sa.Column("source_kind", sa.String(16), nullable=False),
            sa.Column("source_id", sa.String(20), nullable=False),
            sa.Column("source_revision", sa.String(128), nullable=False),
            sa.Column("run_id", sa.String(36), nullable=False),
            sa.PrimaryKeyConstraint(
                "conversation_id", "generation", "source_kind", "source_id", "source_revision"
            ),
            sa.ForeignKeyConstraint(
                ["conversation_id", "generation"],
                ["autonomy_bindings.conversation_id", "autonomy_bindings.generation"],
                ondelete="RESTRICT",
            ),
            sa.ForeignKeyConstraint(
                ["run_id"], ["autonomy_initiative_runs.id"], ondelete="RESTRICT"
            ),
            sa.CheckConstraint("source_kind IN ('event', 'memory')"),
        )
        op.create_index("ix_autonomy_sources_run", "autonomy_source_claims", ["run_id"])
    if "autonomy_initiative_feedback" not in existing:
        op.create_table(
            "autonomy_initiative_feedback",
            sa.Column("run_id", sa.String(36), nullable=False),
            sa.Column("sequence", sa.Integer(), nullable=False),
            sa.Column("outcome", sa.String(16), nullable=False),
            sa.Column("payload_json", sa.Text(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("run_id", "sequence"),
            sa.ForeignKeyConstraint(
                ["run_id"], ["autonomy_initiative_runs.id"], ondelete="RESTRICT"
            ),
            sa.CheckConstraint("sequence >= 1"),
        )


def downgrade() -> None:
    # Preserve accepted run identities, considered sources and receipts on code rollback.
    pass
