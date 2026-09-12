"""Store bounded versioned model-input projections independently of the ledger."""

import sqlalchemy as sa
from alembic import op

from qq_ai_bot.conversation.projection_schema import PROJECTION_TRIGGERS_0054

revision = "0054"
down_revision = "0053"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if "progress_json" not in {
        column["name"] for column in sa.inspect(op.get_bind()).get_columns("sandbox_task_runs")
    }:
        op.add_column(
            "sandbox_task_runs",
            sa.Column("progress_json", sa.Text(), nullable=False, server_default="{}"),
        )
    # Retained on downgrade: these are execution receipts, not a disposable
    # prompt cache. Re-upgrade must not reset an uncertain/delivered attempt.
    if "sandbox_task_continuations" not in sa.inspect(op.get_bind()).get_table_names():
        op.create_table(
            "sandbox_task_continuations",
            sa.Column(
                "request_id",
                sa.String(256),
                sa.ForeignKey("sandbox_task_runs.request_id", ondelete="RESTRICT"),
                primary_key=True,
            ),
            sa.Column("state", sa.String(16), nullable=False),
            sa.Column("claim_token", sa.String(36)),
            sa.Column("attempts", sa.Integer(), nullable=False),
            sa.Column("reason", sa.String(64)),
            sa.Column("outcome_json", sa.Text()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.CheckConstraint(
                "state IN ('ready','claimed','observed','finished','uncertain','blocked')",
                name="ck_sandbox_continuation_state",
            ),
            sa.CheckConstraint("attempts >= 0", name="ck_sandbox_continuation_attempts"),
        )
    if "outcome_json" not in {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns("sandbox_task_continuations")
    }:
        op.add_column("sandbox_task_continuations", sa.Column("outcome_json", sa.Text()))
    op.execute(
        "INSERT INTO sandbox_task_continuations "
        "(request_id,state,claim_token,attempts,reason,updated_at) "
        "SELECT request_id,'uncertain',NULL,0,'legacy_completion',updated_at "
        "FROM sandbox_task_runs WHERE status='completed' AND request_id NOT IN "
        "(SELECT request_id FROM sandbox_task_continuations)"
    )
    op.add_column(
        "canonical_conversations",
        sa.Column(
            "prompt_source_revision",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
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
        sa.Column("source_revision", sa.Integer(), nullable=False),
        sa.Column("starts_after_event_id", sa.Integer(), nullable=False),
        sa.Column("epoch_id", sa.String(36), nullable=False),
        sa.Column("context_key", sa.String(64), nullable=False),
        sa.Column("contract_revision", sa.String(64), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("rebuild_reason", sa.String(32), nullable=False),
        sa.Column("invalidated_reason", sa.String(32)),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("revision >= 1 AND byte_size >= 2", name="ck_prompt_projection_size"),
    )
    op.create_index(
        "ix_prompt_projections_conversation_id", "prompt_projections", ["conversation_id"]
    )
    for statement in PROJECTION_TRIGGERS_0054.values():
        op.execute(statement)


def downgrade() -> None:
    for name in PROJECTION_TRIGGERS_0054:
        op.execute(f"DROP TRIGGER {name}")
    op.drop_index("ix_prompt_projections_conversation_id", table_name="prompt_projections")
    op.drop_table("prompt_projections")
    op.drop_column("canonical_conversations", "prompt_source_revision")
