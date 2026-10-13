"""Retire unused reflection state diagnostics without rewriting source/run facts."""

from alembic import op

revision = "0115"
down_revision = "0114"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for column in (
        "has_yuki_reply",
        "has_tool_result",
        "high_value_signal",
        "last_policy_reason",
        "last_policy_event_id",
    ):
        op.drop_column("memory_self_reflection_states", column)


def downgrade() -> None:
    # Removed diagnostics do not carry execution facts and cannot be reconstructed.
    pass
