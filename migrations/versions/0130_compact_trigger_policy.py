"""Retire Host input-window overrides without reinterpreting their capacity values."""

from alembic import op

revision = "0130"
down_revision = "0129"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.get_bind().exec_driver_sql(
        "DELETE FROM runtime_config_overrides WHERE config_key IN (?, ?)",
        ("context.window_tokens", "context.work_window_tokens"),
    )


def downgrade() -> None:
    raise RuntimeError("Retired Host input-window overrides cannot be reconstructed")
