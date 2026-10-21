"""Retire the old emoji pool; the shared sticker library consists of files only."""

from alembic import op

revision = "0138"
down_revision = "0137"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DELETE FROM runtime_config_overrides WHERE config_key LIKE 'emoji.%'")
    # Pool membership and classifier jobs are retired. Paid calls and delivery
    # receipts live in their existing domains and are intentionally untouched.
    for name in (
        "emoji_usage_events",
        "emoji_jobs",
        "emoji_scope_states",
        "emoji_assets",
        "emoji_descriptions",
    ):
        op.drop_table(name)


def downgrade() -> None:
    raise RuntimeError(
        "The retired emoji library was explicitly cleared; restore code without restoring old user data."
    )
