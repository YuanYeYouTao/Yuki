"""Retire old detached claims once without rewriting execution receipts."""

from alembic import op

revision = "0116"
down_revision = "0115"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "UPDATE sandbox_task_continuations "
        "SET state='blocked', reason='legacy_continuation_retired', updated_at=CURRENT_TIMESTAMP "
        "WHERE state='claimed'"
    )


def downgrade() -> None:
    # Retired claims cannot be restored to permission to repeat an external effect.
    pass
