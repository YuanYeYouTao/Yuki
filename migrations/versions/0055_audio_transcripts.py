"""Durable voice transcripts with history search and prompt invalidation."""

import sqlalchemy as sa
from alembic import op

from qq_ai_bot.asr.schema import CHAT_FTS_0055, PROJECTION_TRIGGERS_0055

revision = "0055"
down_revision = "0054"
branch_labels = None
depends_on = None


def _drop_fts() -> None:
    for suffix in ("ai", "ad", "au"):
        op.execute(f"DROP TRIGGER IF EXISTS chat_events_fts_{suffix}")
    op.execute("DROP TABLE IF EXISTS chat_events_fts")


def upgrade() -> None:
    if "audio_transcript" not in {
        item["name"] for item in sa.inspect(op.get_bind()).get_columns("chat_events")
    }:
        op.add_column(
            "chat_events",
            sa.Column("audio_transcript", sa.Text(), nullable=False, server_default=""),
        )
    _drop_fts()
    for statement in CHAT_FTS_0055:
        op.execute(statement)
    op.execute("INSERT INTO chat_events_fts(chat_events_fts) VALUES ('rebuild')")
    op.execute(PROJECTION_TRIGGERS_0055["prompt_projection_audio_update"])


def downgrade() -> None:
    # Preserve recognized user speech on rollback; the old application ignores the column.
    op.execute("DROP TRIGGER IF EXISTS prompt_projection_audio_update")
    _drop_fts()
    for statement in CHAT_FTS_0055:
        op.execute(
            statement.replace("content, audio_transcript", "content")
            .replace(", new.audio_transcript", "")
            .replace(", old.audio_transcript", "")
        )
    op.execute("INSERT INTO chat_events_fts(chat_events_fts) VALUES ('rebuild')")
