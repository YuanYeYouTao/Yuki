"""Index canonical relationship evidence in event order."""

from alembic import op

revision = "0065"
down_revision = "0064"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_chat_events_conversation_author_id",
        "chat_events",
        ["canonical_conversation_id", "author_person_id", "id"],
    )


def downgrade() -> None:
    op.drop_index("ix_chat_events_conversation_author_id", table_name="chat_events")
