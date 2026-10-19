"""Index public Work steps; root lists reuse the existing state/update index."""

from alembic import op

revision = "0144"
down_revision = "0143"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE INDEX ix_runtime_work_tool_steps ON runtime_work_effects(work_id, created, effect_key, updated) WHERE kind='tool'"
    )


def downgrade() -> None:
    op.drop_index("ix_runtime_work_tool_steps", table_name="runtime_work_effects")
