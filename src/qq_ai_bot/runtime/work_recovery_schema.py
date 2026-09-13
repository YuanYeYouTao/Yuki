"""Durable activation exits and independently recoverable delivery intents."""

import sqlalchemy as sa

from qq_ai_bot.persistence.models import Base

recovery = sa.Table(
    "runtime_work_recovery", Base.metadata,
    sa.Column("work_id", sa.ForeignKey("runtime_work.id", ondelete="CASCADE"), primary_key=True),
    sa.Column("activation_id", sa.String(36), nullable=False),
    sa.Column("exit_reason", sa.String(64), nullable=False, server_default="running"),
    sa.Column("stage", sa.String(32), nullable=False, server_default="activation"),
    sa.Column("failure_json", sa.Text, nullable=False, server_default="{}"),
    sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
    sa.Column("not_before", sa.Float, nullable=False, server_default="0"),
    sa.Column("updated", sa.Float, nullable=False),
    sa.Index("ix_work_recovery_ready", "not_before", "work_id"),
)

deliveries = sa.Table(
    "runtime_delivery_intents", Base.metadata,
    sa.Column("id", sa.String(256), primary_key=True),
    sa.Column("work_id", sa.ForeignKey("runtime_work.id", ondelete="CASCADE"), nullable=False),
    sa.Column("kind", sa.String(24), nullable=False),
    sa.Column("state", sa.String(24), nullable=False),
    sa.Column("payload_json", sa.Text, nullable=False, server_default="{}"),
    sa.Column("receipt_json", sa.Text, nullable=False, server_default="{}"),
    sa.Column("not_before", sa.Float, nullable=False, server_default="0"),
    sa.Column("created", sa.Float, nullable=False),
    sa.Column("updated", sa.Float, nullable=False),
    sa.Index("ix_delivery_work_state", "work_id", "state"),
)
