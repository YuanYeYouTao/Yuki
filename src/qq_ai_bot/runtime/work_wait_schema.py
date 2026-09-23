"""Version 0071 one-shot signal subscriptions for durable Work."""

import sqlalchemy as sa

from qq_ai_bot.persistence.models import Base

waits = sa.Table(
    "runtime_work_waits",
    Base.metadata,
    sa.Column("id", sa.String(36), primary_key=True),
    sa.Column("work_id", sa.ForeignKey("runtime_work.id", ondelete="RESTRICT"), nullable=False),
    sa.Column(
        "conversation_id",
        sa.ForeignKey("canonical_conversations.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column("generation", sa.Integer, nullable=False),
    sa.Column("principal_kind", sa.String(8), nullable=False),
    sa.Column("principal_id", sa.String(36), nullable=False),
    sa.Column("call_key", sa.String(256), nullable=False, unique=True),
    sa.Column("request_json", sa.Text, nullable=False),
    sa.Column("mode", sa.String(3), nullable=False),
    sa.Column("conditions_json", sa.Text, nullable=False),
    sa.Column("status", sa.String(16), nullable=False),
    sa.Column("deadline", sa.Float),
    sa.Column("created", sa.Float, nullable=False),
    sa.Column("updated", sa.Float, nullable=False),
    sa.Column("delivered", sa.Float),
    sa.CheckConstraint("mode IN ('any','all')", name="ck_runtime_work_wait_mode"),
    sa.CheckConstraint(
        "status IN ('active','delivered','cancelled','expired','invalidated')",
        name="ck_runtime_work_wait_status",
    ),
    sa.CheckConstraint(
        "(principal_kind = 'self' AND principal_id = 'self') OR "
        "(principal_kind = 'person' AND principal_id != '' AND principal_id != 'self')",
        name="ck_runtime_work_wait_principal",
    ),
    sa.Index(
        "uq_runtime_work_wait_active",
        "work_id",
        unique=True,
        sqlite_where=sa.text("status = 'active'"),
    ),
    sa.Index("ix_runtime_work_wait_scope", "conversation_id", "generation", "status"),
    sa.Index("ix_runtime_work_wait_deadline", "status", "deadline"),
)
