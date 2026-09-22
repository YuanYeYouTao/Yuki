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
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())
    tool_columns = {
        column["name"]: column for column in inspector.get_columns("memory_tool_receipts")
    }
    tool_checks = {
        check["name"] for check in inspector.get_check_constraints("memory_tool_receipts")
    }
    tool_indexes = {index["name"] for index in inspector.get_indexes("memory_tool_receipts")}
    tool_has_fk = any(
        key["constrained_columns"] == ["initiative_run_id"]
        and key["referred_table"] == "autonomy_initiative_runs"
        and key["referred_columns"] == ["id"]
        for key in inspector.get_foreign_keys("memory_tool_receipts")
    )
    # Code rollback retains these columns and evidence. Replaying this upgrade
    # must not add existing columns again or rebuild already-current tables.
    with op.batch_alter_table("memory_tool_receipts") as batch:
        if not tool_columns["trigger_event_id"]["nullable"]:
            batch.alter_column("trigger_event_id", existing_type=sa.Integer(), nullable=True)
        for name, size in (
            ("initiative_run_id", 36),
            ("tool_call_id", 255),
            ("execution_id", 255),
            ("source_call_key", 64),
        ):
            if name not in tool_columns:
                batch.add_column(sa.Column(name, sa.String(size), nullable=True))
        if not tool_has_fk:
            batch.create_foreign_key(
                "fk_tool_receipt_initiative",
                "autonomy_initiative_runs",
                ["initiative_run_id"],
                ["id"],
                ondelete="RESTRICT",
            )
        if "ck_memory_tool_receipts_source" not in tool_checks:
            batch.create_check_constraint(
                "ck_memory_tool_receipts_source",
                "(trigger_event_id IS NOT NULL AND initiative_run_id IS NULL) OR (trigger_event_id IS NULL AND initiative_run_id IS NOT NULL)",
            )
        if "ck_memory_tool_receipts_initiative_call" not in tool_checks:
            batch.create_check_constraint(
                "ck_memory_tool_receipts_initiative_call",
                "initiative_run_id IS NULL OR (tool_call_id IS NOT NULL AND execution_id IS NOT NULL AND source_call_key IS NOT NULL AND canonical_person_id IS NULL)",
            )
        if "uq_memory_tool_receipts_source_call" not in tool_indexes:
            batch.create_index(
                "uq_memory_tool_receipts_source_call", ["source_call_key"], unique=True
            )
        if "ix_memory_tool_receipts_initiative" not in tool_indexes:
            batch.create_index("ix_memory_tool_receipts_initiative", ["initiative_run_id", "id"])
    mutation_columns = {
        column["name"] for column in inspector.get_columns("memory_mutation_receipts")
    }
    mutation_checks = {
        check["name"]: check["sqltext"]
        for check in inspector.get_check_constraints("memory_mutation_receipts")
    }
    mutation_has_fk = any(
        key["constrained_columns"] == ["initiative_run_id"]
        and key["referred_table"] == "autonomy_initiative_runs"
        and key["referred_columns"] == ["id"]
        for key in inspector.get_foreign_keys("memory_mutation_receipts")
    )
    with op.batch_alter_table("memory_mutation_receipts") as batch:
        if "initiative_run_id" not in mutation_columns:
            batch.add_column(sa.Column("initiative_run_id", sa.String(36), nullable=True))
        if not mutation_has_fk:
            batch.create_foreign_key(
                "fk_mutation_receipt_initiative",
                "autonomy_initiative_runs",
                ["initiative_run_id"],
                ["id"],
                ondelete="RESTRICT",
            )
        source_check = "ck_memory_mutation_trigger_source"
        if "initiative_run_id" not in mutation_checks.get(source_check, ""):
            if source_check in mutation_checks:
                batch.drop_constraint(source_check, type_="check")
            batch.create_check_constraint(
                source_check,
                "(trigger_source_type = 'chat_event' AND trigger_event_id IS NOT NULL AND dream_operation_id IS NULL AND initiative_run_id IS NULL) OR (trigger_source_type = 'dream_operation' AND trigger_event_id IS NULL AND dream_operation_id IS NOT NULL AND initiative_run_id IS NULL) OR (trigger_source_type = 'initiative_run' AND trigger_event_id IS NULL AND dream_operation_id IS NULL AND initiative_run_id IS NOT NULL)",
            )
    if "memory_initiative_reflection_cursors" not in tables:
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
    if "memory_initiative_reflection_windows" not in tables:
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
    # Code rollback retains evidence, call deduplication and reflection watermarks.
    # Downgrading these tables would erase receipts or make actorless rows invalid.
    pass
