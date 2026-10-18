"""Keep recoverable illustration input beside the existing Social receipts."""

import sqlalchemy as sa
from alembic import op

revision = "0139"
down_revision = "0138"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "social_emoji_intents",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("source_turn_id", sa.String(128), nullable=False),
        sa.Column("tool_call_id", sa.String(128), nullable=False),
        sa.Column(
            "source_conversation_id",
            sa.String(36),
            sa.ForeignKey("canonical_conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.Column("target_kind", sa.String(16), nullable=False),
        sa.Column("target_id", sa.String(36), nullable=False),
        sa.Column("source_json", sa.Text, nullable=False),
        sa.Column("intent_json", sa.Text, nullable=False),
        sa.Column("selected_json", sa.Text, nullable=True),
        sa.Column("closed", sa.Boolean, nullable=False, server_default=sa.text("0")),
        sa.Column("error_category", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("source_turn_id", "tool_call_id", name="uq_social_emoji_call"),
    )
    op.create_index(
        "ix_social_emoji_pending", "social_emoji_intents", ["closed", "created_at", "id"]
    )


def downgrade() -> None:
    op.drop_table("social_emoji_intents")
