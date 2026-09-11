"""Store bounded versioned model-input projections independently of the ledger."""

import sqlalchemy as sa
from alembic import op

revision = "0054"
down_revision = "0053"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "prompt_projections",
        sa.Column("view_key", sa.String(64), primary_key=True),
        sa.Column(
            "conversation_id",
            sa.String(36),
            sa.ForeignKey("canonical_conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("starts_after_event_id", sa.Integer(), nullable=False),
        sa.Column("epoch_id", sa.String(36), nullable=False),
        sa.Column("context_key", sa.String(64), nullable=False),
        sa.Column("contract_revision", sa.String(64), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("rebuild_reason", sa.String(32), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("revision >= 1 AND byte_size >= 2", name="ck_prompt_projection_size"),
    )
    op.create_index(
        "ix_prompt_projections_conversation_id", "prompt_projections", ["conversation_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_prompt_projections_conversation_id", table_name="prompt_projections")
    op.drop_table("prompt_projections")
