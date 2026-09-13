"""Version 0060 run-level budget shared by all main work in an automation run."""

import sqlalchemy as sa

from qq_ai_bot.persistence.models import Base

budgets = sa.Table(
    "runtime_automation_budgets",
    Base.metadata,
    sa.Column("run_id", sa.ForeignKey("automation_runs.id", ondelete="CASCADE"), primary_key=True),
    sa.Column("models", sa.Integer, nullable=False, server_default="0"),
    sa.Column("tools", sa.Integer, nullable=False, server_default="0"),
    sa.CheckConstraint(
        "models >= 0 AND tools >= 0",
        name="ck_automation_work_budget",
    ),
)
