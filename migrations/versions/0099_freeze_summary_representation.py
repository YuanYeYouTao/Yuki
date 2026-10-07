"""Freeze summary representation; legacy rows rebuild without inferred labels."""

import sqlalchemy as sa
from alembic import op

revision = "0099"
down_revision = "0098"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "prompt_projections", sa.Column("selected_summary_kind", sa.String(32), nullable=True)
    )
    op.add_column(
        "prompt_projections", sa.Column("selected_summary_renderer", sa.Integer(), nullable=True)
    )


def downgrade() -> None:
    # Derived input may be rebuilt, but an older reader must never silently
    # relabel a committed body after losing its representation metadata.
    op.execute("UPDATE prompt_projections SET invalidated_reason='contract_changed'")
    # Native DROP COLUMN preserves triggers on other tables which refer to
    # prompt_projections; a batch table replacement leaves a dangling trigger.
    op.drop_column("prompt_projections", "selected_summary_renderer")
    op.drop_column("prompt_projections", "selected_summary_kind")
