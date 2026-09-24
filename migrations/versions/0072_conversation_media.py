"""Index event-bound conversation media and its fixed-lifetime cache."""

import sqlalchemy as sa
from alembic import op

revision = "0072"
down_revision = "0071"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Historical scripts and receipts are audit records. Only enabled legacy
    # sends would require a live migration; refuse those before changing schema.
    connection = op.get_bind()
    enabled_legacy = connection.scalar(
        sa.text(
            "SELECT COUNT(*) FROM automations WHERE status IN ('active', 'paused') "
            "AND (script_json LIKE '%onebot.send_private_message%' "
            "OR script_json LIKE '%onebot.send_group_message%' "
            "OR script_json LIKE '%speech.send_private%' "
            "OR script_json LIKE '%speech.send_group%' "
            "OR script_json LIKE '%emoji.send%' "
            "OR script_json LIKE '%onebot.call_api%')"
        )
    )
    if enabled_legacy:
        raise RuntimeError("enabled_legacy_send_requires_review")
    op.create_table(
        "conversation_media_items",
        sa.Column(
            "source_event_id",
            sa.Integer(),
            sa.ForeignKey("chat_events.id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column("attachment_index", sa.Integer(), primary_key=True),
        sa.Column(
            "conversation_id",
            sa.String(36),
            sa.ForeignKey("canonical_conversations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("segment_index", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("display_name", sa.String(80), nullable=False, server_default=""),
        sa.Column("declared_size", sa.Integer()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("cache_status", sa.String(16), nullable=False, server_default="uncached"),
        sa.Column("content_sha256", sa.String(64)),
        sa.Column("cached_at", sa.DateTime(timezone=True)),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("cache_name", sa.String(100)),
        sa.CheckConstraint(
            "attachment_index >= 0 AND segment_index >= 0", name="ck_conversation_media_indices"
        ),
        sa.CheckConstraint(
            "cache_status IN ('uncached', 'cached', 'expired')", name="ck_conversation_media_status"
        ),
    )
    op.create_index(
        "ix_conversation_media_scope_event",
        "conversation_media_items",
        ["conversation_id", "source_event_id"],
    )
    op.create_index("ix_conversation_media_expiry", "conversation_media_items", ["expires_at"])
    op.create_table(
        "canonical_generation_reset_batches",
        sa.Column("batch_id", sa.String(64), primary_key=True),
        sa.Column(
            "conversation_id",
            sa.String(36),
            sa.ForeignKey("canonical_conversations.id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column("prior_generation", sa.Integer(), nullable=False),
        sa.Column("new_generation", sa.Integer(), nullable=False),
        sa.Column("floor_event_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("canonical_generation_reset_batches")
    op.drop_index("ix_conversation_media_expiry", table_name="conversation_media_items")
    op.drop_index("ix_conversation_media_scope_event", table_name="conversation_media_items")
    op.drop_table("conversation_media_items")
