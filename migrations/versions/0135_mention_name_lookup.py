"""Index the latest observed name in one original conversation."""

import sqlalchemy as sa
from alembic import op

revision = "0135"
down_revision = "0134"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_chat_events_conversation_sender_time",
        "chat_events",
        ["canonical_conversation_id", "sender_user_id", "occurred_at", "id"],
        sqlite_where=sa.text("suppression_status = 'keeper'"),
    )


def downgrade() -> None:
    op.drop_index("ix_chat_events_conversation_sender_time", table_name="chat_events")
