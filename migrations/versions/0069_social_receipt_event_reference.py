"""Persist confirmed Social delivery's internal ledger event without guessing history."""

import sqlalchemy as sa
from alembic import op

revision = "0069"
down_revision = "0068"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "event_id" not in {
        column["name"] for column in inspector.get_columns("social_operation_receipts")
    }:
        # SQLite permits a nullable REFERENCES column without rebuilding the
        # durable receipt table. Old rows deliberately remain unlinked.
        op.execute(
            "ALTER TABLE social_operation_receipts ADD COLUMN event_id INTEGER "
            "REFERENCES chat_events(id) ON UPDATE RESTRICT ON DELETE SET NULL"
        )
    if "ix_social_operation_event_id" not in {
        index["name"] for index in inspector.get_indexes("social_operation_receipts")
    }:
        op.create_index("ix_social_operation_event_id", "social_operation_receipts", ["event_id"])


def downgrade() -> None:
    # Code rollback retains confirmed event anchors and all send receipts.
    pass
