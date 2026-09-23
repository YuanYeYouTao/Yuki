"""Persist source-free SELF initiative and its conversational thread."""

import sqlalchemy as sa
from alembic import op

revision = "0070"
down_revision = "0069"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns("autonomy_initiative_runs")
    }
    if "trigger_kind" not in columns:
        op.execute(
            "ALTER TABLE autonomy_initiative_runs ADD COLUMN trigger_kind VARCHAR(16) NOT NULL DEFAULT 'source'"
        )
    if "thread_key" not in columns:
        op.execute("ALTER TABLE autonomy_initiative_runs ADD COLUMN thread_key VARCHAR(256)")


def downgrade() -> None:
    # Retain acceptance identities and thread evidence across code rollback.
    pass
