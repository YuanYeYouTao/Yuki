"""Worker execution records and Work media; relations belong to Work."""

import sqlalchemy as sa

from qq_ai_bot.runtime.work_budget_schema import budgets as budgets
from qq_ai_bot.runtime.work_schema_v1 import work

children = sa.Table(
    "runtime_subagents",
    work.metadata,
    sa.Column("work_id", sa.ForeignKey("runtime_work.id", ondelete="RESTRICT"), primary_key=True),
    sa.Column("source_key", sa.String(256), nullable=False, unique=True),
    sa.Column("brief_json", sa.Text, nullable=False),
    sa.Column("result_json", sa.Text, nullable=False, server_default="{}"),
    sa.Column("owner", sa.String(36)),
    sa.Column("fence", sa.Integer, nullable=False, server_default="0"),
    sa.Column("lease_until", sa.Float, nullable=False, server_default="0"),
    sa.Column("cancel_epoch", sa.Integer, nullable=False, server_default="0"),
    sa.Column("notified_revision", sa.Integer, nullable=False, server_default="0"),
    sa.Column("archived_at", sa.Float),
)

media = sa.Table(
    "runtime_work_media",
    work.metadata,
    sa.Column("sha256", sa.String(64), primary_key=True),
    sa.Column("content", sa.LargeBinary, nullable=False),
)

media_refs = sa.Table(
    "runtime_work_media_refs",
    work.metadata,
    sa.Column("work_id", sa.ForeignKey("runtime_work.id", ondelete="CASCADE"), primary_key=True),
    sa.Column("sha256", sa.ForeignKey("runtime_work_media.sha256"), primary_key=True),
)
