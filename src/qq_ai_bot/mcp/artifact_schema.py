"""Reference owners for retained research results, without execution state."""

import sqlalchemy as sa

from qq_ai_bot.persistence.models import Base

artifact_refs = sa.Table(
    "tool_artifact_refs",
    Base.metadata,
    sa.Column("owner_kind", sa.String(24), primary_key=True),
    sa.Column("owner_id", sa.String(128), primary_key=True),
    sa.Column(
        "handle_id",
        sa.ForeignKey("tool_artifacts.handle_id", ondelete="CASCADE"),
        primary_key=True,
    ),
    sa.Index("ix_tool_artifact_refs_handle", "handle_id"),
)
