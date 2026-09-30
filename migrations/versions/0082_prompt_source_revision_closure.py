"""Fence event metadata and the old owner of moved rollups with the existing revision."""

from alembic import op

from qq_ai_bot.conversation.projection_revision_schema import PROJECTION_ADDITIONS_0082

revision = "0082"
down_revision = "0081"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for statement in PROJECTION_ADDITIONS_0082.values():
        op.execute(statement)


def downgrade() -> None:
    for name in PROJECTION_ADDITIONS_0082:
        op.execute(f"DROP TRIGGER IF EXISTS {name}")
