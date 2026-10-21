"""Publish the complete offline history index and retire its old runtime objects."""

from alembic import op

revision = "0145"
down_revision = "0144"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for name in ("chat_events_fts_ai", "chat_events_fts_au", "chat_events_fts_ad"):
        op.execute(f"DROP TRIGGER IF EXISTS {name}")
    op.execute("DROP TABLE chat_events_fts")
    op.execute(
        "CREATE TRIGGER chat_history_fts_ad AFTER DELETE ON chat_events BEGIN DELETE FROM chat_history_fts WHERE rowid=OLD.id; END"
    )


def downgrade() -> None:
    raise RuntimeError("0145 replaces history matching and its derived index")
