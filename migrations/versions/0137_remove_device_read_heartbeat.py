"""Delete device heartbeat writes from authentication reads."""

import sqlalchemy as sa
from alembic import op

revision = "0137"
down_revision = "0136"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("client_devices", "last_used_at")


def downgrade() -> None:
    op.add_column("client_devices", sa.Column("last_used_at", sa.DateTime(timezone=True)))
