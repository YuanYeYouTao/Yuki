"""Persistent worker identities and atomic root budget accounting."""

from alembic import op
from sqlalchemy import text

from qq_ai_bot.runtime.subagent_schema import TABLES

revision = "0057"
down_revision = "0056"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table in TABLES:
        table.create(op.get_bind(), checkfirst=True)
    op.get_bind().execute(
        text(
            "INSERT OR IGNORE INTO runtime_work_budgets (root_id, models, tools) "
            "SELECT id, model_requests, tool_calls FROM runtime_work"
        )
    )


def downgrade() -> None:
    # Disable admission and settle workers before rolling back code. Keep receipts.
    pass
