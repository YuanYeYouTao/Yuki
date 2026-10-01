"""Nullable cumulative limits for new work; retain all historical accounting."""

import sqlalchemy as sa
from alembic import op

revision = "0087"
down_revision = "0086"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # A historical Work with no reservation yet still had the old implicit limits.
    op.execute(
        sa.text(
            "INSERT OR IGNORE INTO runtime_work_budgets "
            "(root_id, models, tools, model_limit, tool_limit) "
            "SELECT w.id, w.model_requests, w.tool_calls, 120, 160 FROM runtime_work w "
            "WHERE NOT EXISTS (SELECT 1 FROM runtime_subagents c WHERE c.work_id = w.id)"
        )
    )
    with op.batch_alter_table("runtime_work_budgets") as batch:
        batch.alter_column(
            "model_limit", existing_type=sa.Integer(), nullable=True, server_default=None
        )
        batch.alter_column(
            "tool_limit", existing_type=sa.Integer(), nullable=True, server_default=None
        )
    # Defaults backfill existing run records, then are removed for new runs.
    with op.batch_alter_table("runtime_automation_budgets") as batch:
        batch.add_column(
            sa.Column("model_limit", sa.Integer(), nullable=True, server_default="120")
        )
        batch.add_column(sa.Column("tool_limit", sa.Integer(), nullable=True, server_default="160"))
    op.execute(
        sa.text(
            "INSERT OR IGNORE INTO runtime_automation_budgets "
            "(run_id, models, tools, model_limit, tool_limit) "
            "SELECT id, 0, 0, 120, 160 FROM automation_runs"
        )
    )
    with op.batch_alter_table("runtime_automation_budgets") as batch:
        batch.alter_column("model_limit", existing_type=sa.Integer(), server_default=None)
        batch.alter_column("tool_limit", existing_type=sa.Integer(), server_default=None)


def downgrade() -> None:
    for table in ("runtime_work_budgets", "runtime_automation_budgets"):
        unlimited = op.get_bind().scalar(
            sa.text(
                f"SELECT 1 FROM {table} WHERE model_limit IS NULL OR tool_limit IS NULL LIMIT 1"
            )
        )
        if unlimited:
            raise RuntimeError("unlimited_budget_requires_explicit_compatible_rollback")
    if op.get_bind().scalar(
        sa.text(
            "SELECT 1 FROM runtime_automation_budgets "
            "WHERE model_limit != 120 OR tool_limit != 160 LIMIT 1"
        )
    ):
        raise RuntimeError("automation_budget_requires_explicit_compatible_rollback")
    with op.batch_alter_table("runtime_automation_budgets") as batch:
        batch.drop_column("model_limit")
        batch.drop_column("tool_limit")
    with op.batch_alter_table("runtime_work_budgets") as batch:
        batch.alter_column(
            "model_limit", existing_type=sa.Integer(), nullable=False, server_default="120"
        )
        batch.alter_column(
            "tool_limit", existing_type=sa.Integer(), nullable=False, server_default="160"
        )
