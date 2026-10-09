"""Durable work goals, input consumption and activation fencing."""

from alembic import op
from sqlalchemy.schema import CreateIndex, CreateTable

from qq_ai_bot.runtime.work_schema_v1 import TABLES

revision = "0056"
down_revision = "0055"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table in TABLES:
        op.get_bind().execute(CreateTable(table, if_not_exists=True))
        for index in table.indexes:
            # Work query indexes belong to the frozen 0085/0104 migrations.
            if not index.name.startswith("ix_runtime_work_query_"):
                op.get_bind().execute(CreateIndex(index, if_not_exists=True))


def downgrade() -> None:
    # Old code must not erase goals, input receipts or external-effect evidence.
    # Runtime enablement is disabled before code rollback; preserve additive tables.
    pass
