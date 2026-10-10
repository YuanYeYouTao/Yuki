"""Frozen version 0057 schema; runtime tables may evolve independently."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import text

_metadata = sa.MetaData()
work = sa.Table("runtime_work", _metadata, sa.Column("id", sa.String(36), primary_key=True))

children = sa.Table(
    "runtime_subagents",
    work.metadata,
    sa.Column("work_id", sa.ForeignKey("runtime_work.id", ondelete="RESTRICT"), primary_key=True),
    sa.Column("root_id", sa.ForeignKey("runtime_work.id", ondelete="RESTRICT"), nullable=False),
    sa.Column("source_key", sa.String(256), nullable=False, unique=True),
    sa.Column("brief_json", sa.Text, nullable=False),
    sa.Column("result_json", sa.Text, nullable=False, server_default="{}"),
    sa.Column("owner", sa.String(36)),
    sa.Column("fence", sa.Integer, nullable=False, server_default="0"),
    sa.Column("lease_until", sa.Float, nullable=False, server_default="0"),
    sa.Column("cancel_epoch", sa.Integer, nullable=False, server_default="0"),
    sa.Column("notified_revision", sa.Integer, nullable=False, server_default="0"),
    sa.Column("archived_at", sa.Float),
    sa.Index("ix_runtime_subagents_root", "root_id"),
)

budgets = sa.Table(
    "runtime_work_budgets",
    work.metadata,
    sa.Column("root_id", sa.ForeignKey("runtime_work.id", ondelete="CASCADE"), primary_key=True),
    sa.Column("models", sa.Integer, nullable=False, server_default="0"),
    sa.Column("tools", sa.Integer, nullable=False, server_default="0"),
    sa.Column("model_limit", sa.Integer, nullable=False, server_default="120"),
    sa.Column("tool_limit", sa.Integer, nullable=False, server_default="160"),
    sa.CheckConstraint("models >= 0 AND tools >= 0", name="ck_runtime_budget_usage"),
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

TABLES = (children, budgets, media, media_refs)

revision = "0057"
down_revision = "0056"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table in TABLES:
        table.create(op.get_bind(), checkfirst=True)
    op.get_bind().execute(
        text(
            "INSERT OR IGNORE INTO runtime_work_budgets (root_id, models, tools) "
            "SELECT id, model_requests, tool_calls FROM runtime_work"
        )
    )


def downgrade() -> None:
    # Disable admission and settle workers before rolling back code. Keep receipts.
    pass
