"""Actorless SELF tool evidence and independent reflection watermarks.

Revision ID: 0067
Revises: 0066
"""

import sqlalchemy as sa
from alembic import op

revision = "0067"
down_revision = "0066"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("memory_tool_receipts") as batch:
        batch.alter_column("trigger_event_id", existing_type=sa.Integer(), nullable=True)
        batch.add_column(sa.Column("initiative_run_id", sa.String(36), nullable=True))
        batch.add_column(sa.Column("tool_call_id", sa.String(255), nullable=True))
        batch.add_column(sa.Column("execution_id", sa.String(255), nullable=True))
        batch.add_column(sa.Column("source_call_key", sa.String(64), nullable=True))
        batch.create_foreign_key(
            "fk_tool_receipt_initiative",
            "autonomy_initiative_runs",
            ["initiative_run_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch.create_check_constraint(
            "ck_memory_tool_receipts_source",
            "(trigger_event_id IS NOT NULL AND initiative_run_id IS NULL) OR (trigger_event_id IS NULL AND initiative_run_id IS NOT NULL)",
        )
        batch.create_check_constraint(
            "ck_memory_tool_receipts_initiative_call",
            "initiative_run_id IS NULL OR (tool_call_id IS NOT NULL AND execution_id IS NOT NULL AND source_call_key IS NOT NULL AND canonical_person_id IS NULL)",
        )
        batch.create_index("uq_memory_tool_receipts_source_call", ["source_call_key"], unique=True)
        batch.create_index("ix_memory_tool_receipts_initiative", ["initiative_run_id", "id"])
    with op.batch_alter_table("memory_mutation_receipts") as batch:
        batch.add_column(sa.Column("initiative_run_id", sa.String(36), nullable=True))
        batch.create_foreign_key(
            "fk_mutation_receipt_initiative",
            "autonomy_initiative_runs",
            ["initiative_run_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch.drop_constraint("ck_memory_mutation_trigger_source", type_="check")
        batch.create_check_constraint(
            "ck_memory_mutation_trigger_source",
            "(trigger_source_type = 'chat_event' AND trigger_event_id IS NOT NULL AND dream_operation_id IS NULL AND initiative_run_id IS NULL) OR (trigger_source_type = 'dream_operation' AND trigger_event_id IS NULL AND dream_operation_id IS NOT NULL AND initiative_run_id IS NULL) OR (trigger_source_type = 'initiative_run' AND trigger_event_id IS NULL AND dream_operation_id IS NULL AND initiative_run_id IS NOT NULL)",
        )
    op.create_table(
        "memory_initiative_reflection_cursors",
        sa.Column(
            "initiative_run_id",
            sa.String(36),
            sa.ForeignKey("autonomy_initiative_runs.id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column("last_receipt_id", sa.Integer(), nullable=False),
    )
    op.create_table(
        "memory_initiative_reflection_windows",
        sa.Column(
            "reflection_run_id",
            sa.Integer(),
            sa.ForeignKey("memory_self_reflection_runs.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "initiative_run_id",
            sa.String(36),
            sa.ForeignKey("autonomy_initiative_runs.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("first_receipt_id", sa.Integer(), nullable=False),
        sa.Column("last_receipt_id", sa.Integer(), nullable=False),
        sa.UniqueConstraint("initiative_run_id", "first_receipt_id"),
    )


def downgrade() -> None:
    raise RuntimeError(
        "0067 preserves actorless evidence; restore a consistent backup to downgrade"
    )
