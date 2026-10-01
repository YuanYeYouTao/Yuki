"""Current query contracts; historical 0057/0060 table definitions stay frozen."""

import sqlalchemy as sa

_metadata = sa.MetaData()
sa.Table("runtime_work", _metadata, sa.Column("id", sa.String(36), primary_key=True))
sa.Table("automation_runs", _metadata, sa.Column("id", sa.Integer, primary_key=True))
budgets = sa.Table(
    "runtime_work_budgets",
    _metadata,
    sa.Column("root_id", sa.ForeignKey("runtime_work.id", ondelete="CASCADE"), primary_key=True),
    sa.Column("models", sa.Integer, nullable=False, server_default="0"),
    sa.Column("tools", sa.Integer, nullable=False, server_default="0"),
    sa.Column("model_limit", sa.Integer, nullable=True),
    sa.Column("tool_limit", sa.Integer, nullable=True),
    sa.CheckConstraint("models >= 0 AND tools >= 0", name="ck_runtime_budget_usage"),
)
automation_budgets = sa.Table(
    "runtime_automation_budgets",
    _metadata,
    sa.Column("run_id", sa.ForeignKey("automation_runs.id", ondelete="CASCADE"), primary_key=True),
    sa.Column("models", sa.Integer, nullable=False, server_default="0"),
    sa.Column("tools", sa.Integer, nullable=False, server_default="0"),
    sa.Column("model_limit", sa.Integer, nullable=True),
    sa.Column("tool_limit", sa.Integer, nullable=True),
    sa.CheckConstraint("models >= 0 AND tools >= 0", name="ck_automation_work_budget"),
)


def create_current_budget_tables(connection: sa.Connection) -> None:
    """Fresh isolated schemas use current tables; production upgrades use 0087."""
    budgets.create(connection, checkfirst=True)
    automation_budgets.create(connection, checkfirst=True)
