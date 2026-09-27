"""Expiring execution diagnostics, independent of recovery checkpoints."""

import sqlalchemy as sa
from alembic import op

revision = "0074"
down_revision = "0073"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "execution_trace_state",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("privacy_generation", sa.Integer(), nullable=False),
    )
    op.create_table(
        "execution_trace_entries",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "conversation_id",
            sa.String(36),
            sa.ForeignKey("canonical_conversations.id", ondelete="CASCADE"),
        ),
        sa.Column("turn_id", sa.String(64), nullable=False),
        sa.Column("operation_id", sa.String(36), nullable=False),
        sa.Column("parent_operation_id", sa.String(36)),
        sa.Column("work_id", sa.String(36)),
        sa.Column("activation_id", sa.String(64)),
        sa.Column("execution_id", sa.String(128)),
        sa.Column(
            "source_event_id", sa.Integer(), sa.ForeignKey("chat_events.id", ondelete="SET NULL")
        ),
        sa.Column("generation", sa.Integer()),
        sa.Column("origin", sa.String(64)),
        sa.Column("kind", sa.String(40), nullable=False),
        sa.Column("payload_status", sa.String(24), nullable=False),
        sa.Column("payload_gzip", sa.LargeBinary()),
        sa.Column("payload_sha256", sa.String(64)),
        sa.Column("payload_bytes", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sqlite_autoincrement=True,
    )
    for name, columns in (
        ("conversation_id", ["conversation_id", "id"]),
        ("turn_id", ["turn_id", "id"]),
        ("work_id", ["work_id", "id"]),
        ("operation_id", ["operation_id", "id"]),
        ("expires", ["expires_at", "id"]),
    ):
        op.create_index(f"ix_execution_trace_{name}", "execution_trace_entries", columns)


def downgrade() -> None:
    # Diagnostics are explicitly disposable; business receipts remain untouched.
    op.drop_table("execution_trace_entries")
    op.drop_table("execution_trace_state")
