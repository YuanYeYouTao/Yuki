"""Keep actual reply originals observed by a receive event without inventing history."""

import sqlalchemy as sa
from alembic import op

revision = "0140"
down_revision = "0139"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("conversation_media_items", sa.Column("source_snapshot_json", sa.Text()))


def downgrade() -> None:
    op.drop_column("conversation_media_items", "source_snapshot_json")
