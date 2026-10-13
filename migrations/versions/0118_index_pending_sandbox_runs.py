"""Index cancellation reconciliation without changing source or completion receipts."""

from alembic import op

revision = "0118"
down_revision = "0117"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE INDEX ix_sandbox_task_runs_waiting_run ON sandbox_task_runs(run_id) "
        "WHERE status='waiting' AND run_id IS NOT NULL"
    )


def downgrade() -> None:
    op.drop_index("ix_sandbox_task_runs_waiting_run", table_name="sandbox_task_runs")
