"""Durable work goals, input consumption and activation fencing."""

from alembic import op

from qq_ai_bot.runtime.work_schema_v1 import TABLES

revision = "0056"
down_revision = "0055"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table in TABLES:
        table.create(op.get_bind(), checkfirst=True)


def downgrade() -> None:
    # Old code must not erase goals, input receipts or external-effect evidence.
    # Runtime enablement is disabled before code rollback; preserve additive tables.
    pass
