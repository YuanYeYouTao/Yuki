"""Retire unused diagnostic tables and execution trace indexes."""

from alembic import op

revision = "0134"
down_revision = "0133"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_table("tool_invocations")
    op.drop_table("runtime_turn_observations")
    op.drop_index("ix_execution_trace_roots", table_name="execution_trace_entries")
    op.drop_index("ix_execution_trace_operation_id", table_name="execution_trace_entries")


def downgrade() -> None:
    raise RuntimeError("Retired diagnostic rows cannot be reconstructed")
