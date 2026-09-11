"""Persist sandbox source anchors before submission and completions before ack."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0053"
down_revision: str | None = "0052"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # A binary rollback retains execution receipts. Re-upgrade must reuse them.
    if "sandbox_task_runs" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "sandbox_task_runs",
        sa.Column("request_id", sa.String(256), primary_key=True),
        sa.Column(
            "source_conversation_id",
            sa.String(36),
            sa.ForeignKey("canonical_conversations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("source_json", sa.Text(), nullable=False),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.Column("run_id", sa.String(36), unique=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("completion_json", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status IN ('waiting','completed')", name="ck_sandbox_task_run_status"),
    )


def downgrade() -> None:
    # Schema 0052 ignores this additional table. Keeping it allows the old Bot
    # to restart without erasing accepted work or creating duplicate effects
    # when the new binary is installed again.
    pass
