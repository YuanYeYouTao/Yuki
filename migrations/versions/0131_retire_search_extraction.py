"""Retire automatic search extraction and duplicate source-index controls."""

from alembic import op

revision = "0131"
down_revision = "0130"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "DELETE FROM runtime_config_overrides WHERE config_key IN ("
        "'web.extract_max_results', 'web.source_max_runs_per_conversation')"
    )


def downgrade() -> None:
    """Retired configuration values cannot be reconstructed."""
