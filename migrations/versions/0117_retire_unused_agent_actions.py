"""Remove the unused generic OneBot diagnostic log, not execution receipts."""

from alembic import op

revision = "0117"
down_revision = "0116"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_table("agent_actions")


def downgrade() -> None:
    raise RuntimeError("Retired diagnostic rows cannot be reconstructed")
