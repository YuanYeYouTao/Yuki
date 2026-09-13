"""Coalesced rollup checks; inspecting history never holds the event writer lock."""

import sqlalchemy as sa

from qq_ai_bot.persistence.models import Base

signals = sa.Table(
    "canonical_rollup_signals",
    Base.metadata,
    sa.Column(
        "conversation_id",
        sa.ForeignKey("canonical_conversations.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    sa.Column("generation", sa.Integer, nullable=False),
    sa.Column("event_id", sa.Integer, nullable=False),
    sa.Column("revision", sa.Integer, nullable=False, server_default="1"),
)
