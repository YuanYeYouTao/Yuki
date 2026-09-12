"""Version 0056 durable work schema. Keep this migration contract immutable."""

import sqlalchemy as sa

from qq_ai_bot.persistence.models import Base

WORK_STATES = (
    "queued",
    "running",
    "waiting_external",
    "waiting_user",
    "suspended",
    "completed",
    "failed",
    "cancelled",
)

work = sa.Table(
    "runtime_work",
    Base.metadata,
    sa.Column("id", sa.String(36), primary_key=True),
    sa.Column(
        "conversation_id",
        sa.ForeignKey("canonical_conversations.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column("generation", sa.Integer, nullable=False),
    sa.Column("source_key", sa.String(256), nullable=False, unique=True),
    sa.Column("source_json", sa.Text, nullable=False),
    sa.Column("goal", sa.Text, nullable=False),
    sa.Column("revision", sa.Integer, nullable=False, server_default="1"),
    sa.Column("state", sa.String(24), nullable=False),
    sa.Column("reason", sa.String(128)),
    sa.Column("model_requests", sa.Integer, nullable=False, server_default="0"),
    sa.Column("tool_calls", sa.Integer, nullable=False, server_default="0"),
    sa.Column("sent_messages", sa.Integer, nullable=False, server_default="0"),
    sa.Column("checkpoint_json", sa.Text, nullable=False, server_default="{}"),
    sa.Column("created", sa.Float, nullable=False),
    sa.Column("updated", sa.Float, nullable=False),
    sa.CheckConstraint(
        "state IN (" + ",".join(repr(s) for s in WORK_STATES) + ")", name="ck_runtime_work_state"
    ),
    sa.CheckConstraint(
        "revision >= 1 AND model_requests >= 0 AND tool_calls >= 0 AND sent_messages >= 0",
        name="ck_runtime_work_counters",
    ),
    sa.Index("ix_runtime_work_scope_state", "conversation_id", "state", "updated"),
)

scope = sa.Table(
    "runtime_work_scopes",
    Base.metadata,
    sa.Column(
        "conversation_id",
        sa.ForeignKey("canonical_conversations.id", ondelete="RESTRICT"),
        primary_key=True,
    ),
    sa.Column("generation", sa.Integer, nullable=False),
    sa.Column("cancel_epoch", sa.Integer, nullable=False, server_default="0"),
    sa.Column("fence", sa.Integer, nullable=False, server_default="0"),
    sa.Column("owner", sa.String(36)),
    sa.Column("lease_until", sa.Float, nullable=False, server_default="0"),
)

inputs = sa.Table(
    "runtime_work_inputs",
    Base.metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column(
        "conversation_id",
        sa.ForeignKey("canonical_conversations.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column("generation", sa.Integer, nullable=False),
    sa.Column("source_key", sa.String(256), nullable=False, unique=True),
    sa.Column("event_id", sa.ForeignKey("chat_events.id", ondelete="SET NULL")),
    sa.Column("work_id", sa.ForeignKey("runtime_work.id", ondelete="RESTRICT")),
    sa.Column("kind", sa.String(24), nullable=False),
    sa.Column("state", sa.String(16), nullable=False, server_default="pending"),
    sa.Column("attempt_id", sa.String(36)),
    sa.Column("created", sa.Float, nullable=False),
    sa.CheckConstraint(
        "state IN ('pending','staged','consumed','cancelled')", name="ck_runtime_work_input_state"
    ),
    sa.Index("ix_runtime_work_inputs_pending", "conversation_id", "state", "id"),
)

effects = sa.Table(
    "runtime_work_effects",
    Base.metadata,
    sa.Column("effect_key", sa.String(256), primary_key=True),
    sa.Column("work_id", sa.ForeignKey("runtime_work.id", ondelete="RESTRICT"), nullable=False),
    sa.Column("kind", sa.String(24), nullable=False),
    sa.Column("state", sa.String(16), nullable=False),
    sa.Column("receipt_json", sa.Text, nullable=False, server_default="{}"),
    sa.Column("created", sa.Float, nullable=False),
    sa.Column("updated", sa.Float, nullable=False),
    sa.CheckConstraint(
        "state IN ('prepared','accepted','failed','unknown')", name="ck_runtime_work_effect_state"
    ),
)

TABLES = (work, scope, inputs, effects)
