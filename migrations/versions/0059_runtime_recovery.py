"""Store activation recovery and delivery intent independently of model prose."""

from alembic import op

from qq_ai_bot.runtime.work_recovery_schema import deliveries, recovery

revision = "0059"
down_revision = "0058"
branch_labels = None
depends_on = None


def upgrade() -> None:
    recovery.create(op.get_bind(), checkfirst=True)
    deliveries.create(op.get_bind(), checkfirst=True)


def downgrade() -> None:
    # Runtime rollback cannot discard accepted receipts or reset task budgets.
    pass
