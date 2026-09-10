"""Add content-free social effect receipts; preserve all existing business data."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0052"
down_revision: str | None = "0051"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "social_operation_receipts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("source_turn_id", sa.String(128), nullable=False),
        sa.Column("tool_call_id", sa.String(128), nullable=False),
        sa.Column(
            "source_conversation_id",
            sa.String(36),
            sa.ForeignKey("canonical_conversations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("action", sa.String(32), nullable=False),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.Column("target_kind", sa.String(16), nullable=False),
        sa.Column("target_id", sa.String(36), nullable=False),
        sa.Column("presence_id", sa.String(36), sa.ForeignKey("presences.id", ondelete="RESTRICT")),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("platform_reference", sa.String(255)),
        sa.Column("error_category", sa.String(64)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("source_turn_id", "tool_call_id", name="uq_social_operation_call"),
        sa.CheckConstraint(
            "status IN ('prepared','executing','succeeded','failed','uncertain')",
            name="ck_social_operation_status",
        ),
        sa.CheckConstraint("target_kind IN ('person','space')", name="ck_social_target_kind"),
    )
    op.create_index(
        "ix_social_operation_target_time", "social_operation_receipts", ["target_id", "created_at"]
    )


def downgrade() -> None:
    # Forgetting uncertain sends would permit duplicate effects after rollback.
    raise RuntimeError("social receipts require snapshot-based rollback")
