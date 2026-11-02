"""Index the existing active initiative outbox in its reconciliation order."""

import sqlalchemy as sa
from alembic import op

revision = "0156"
down_revision = "0155"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_autonomy_runs_reconcile",
        "autonomy_initiative_runs",
        ["created_at"],
        sqlite_where=sa.text("state IN ('accepted', 'running')"),
    )


def downgrade() -> None:
    op.drop_index("ix_autonomy_runs_reconcile", table_name="autonomy_initiative_runs")
