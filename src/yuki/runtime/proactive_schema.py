"""Receipts of Yuki-opened private turns and the watchdog's told-once facts."""

import sqlalchemy as sa

from yuki.persistence.models import Base

PROACTIVE_TURN_STATES = ("pending", "running", "finished", "failed", "unknown")

proactive_turns = sa.Table(
    "proactive_turns",
    Base.metadata,
    sa.Column("id", sa.String(36), primary_key=True),
    sa.Column("source_kind", sa.String(32), nullable=False),
    sa.Column("person_id", sa.ForeignKey("persons.id", ondelete="RESTRICT"), nullable=False),
    sa.Column("conversation_id", sa.String(36)),
    sa.Column("presence_id", sa.String(36)),
    sa.Column("state", sa.String(16), nullable=False),
    sa.Column("reason", sa.String(128)),
    sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
    sa.Column("result_json", sa.Text),
    sa.Column("created", sa.Float, nullable=False),
    sa.Column("started", sa.Float),
    sa.Column("updated", sa.Float, nullable=False),
    sa.CheckConstraint(
        "state IN ('pending', 'running', 'finished', 'failed', 'unknown')",
        name="ck_proactive_turns_state",
    ),
    sa.Index("ix_proactive_turns_state", "state", "updated"),
    sa.Index("ix_proactive_turns_person_started", "person_id", "started"),
)

watchdog_issues = sa.Table(
    "watchdog_issues",
    Base.metadata,
    sa.Column(
        "person_id",
        sa.ForeignKey("persons.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    sa.Column("issue_key", sa.String(256), primary_key=True),
    sa.Column("opened", sa.Float, nullable=False),
    sa.Column("facts_json", sa.Text, nullable=False),
    sa.Column("turn_id", sa.ForeignKey("proactive_turns.id", ondelete="SET NULL")),
    sa.Index("ix_watchdog_issues_turn", "turn_id"),
)
