"""Remove retired outbound social frequency settings."""

from alembic import op

revision = "0062"
down_revision = "0061"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "DELETE FROM runtime_config_overrides WHERE config_key IN ("
        "'social.send_per_target_per_minute', 'social.send_global_per_minute', "
        "'social.poke_per_target_per_minute', 'social.poke_global_per_minute')"
    )


def downgrade() -> None:
    # Removed per-owner settings cannot be reconstructed from the old defaults.
    pass
