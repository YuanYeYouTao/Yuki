"""Budget migration retains historical limits and never guesses unlimited rollback."""

import importlib

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from qq_ai_bot.runtime.work_budget import WorkBudgetExceeded, charge_automation_run
from qq_ai_bot.runtime.work_budget_schema import automation_budgets, create_current_budget_tables


def test_budget_upgrade_preserves_existing_and_unreserved_work():
    migration = importlib.import_module("migrations.versions.0087_unlimited_work_budgets")
    with sa.create_engine("sqlite://").begin() as connection:
        for sql in (
            "CREATE TABLE runtime_work (id TEXT PRIMARY KEY, "
            "model_requests INTEGER, tool_calls INTEGER)",
            "CREATE TABLE runtime_subagents (work_id TEXT PRIMARY KEY)",
            "CREATE TABLE automation_runs (id INTEGER PRIMARY KEY)",
            "CREATE TABLE runtime_work_budgets (root_id TEXT PRIMARY KEY, "
            "models INTEGER NOT NULL DEFAULT 0, tools INTEGER NOT NULL DEFAULT 0, "
            "model_limit INTEGER NOT NULL DEFAULT 120, tool_limit INTEGER NOT NULL DEFAULT 160)",
            "CREATE TABLE runtime_automation_budgets (run_id INTEGER PRIMARY KEY, "
            "models INTEGER NOT NULL DEFAULT 0, tools INTEGER NOT NULL DEFAULT 0)",
            "INSERT INTO runtime_work VALUES ('explicit',7,9),('unreserved',3,4),('child',2,2)",
            "INSERT INTO runtime_subagents VALUES ('child')",
            "INSERT INTO runtime_work_budgets VALUES ('explicit',7,9,10,20)",
            "INSERT INTO automation_runs VALUES (1),(2)",
            "INSERT INTO runtime_automation_budgets VALUES (1,17,19)",
        ):
            connection.exec_driver_sql(sql)
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
            roots = connection.exec_driver_sql(
                "SELECT root_id,models,tools,model_limit,tool_limit FROM runtime_work_budgets "
                "ORDER BY root_id"
            ).all()
            assert roots == [("explicit", 7, 9, 10, 20), ("unreserved", 3, 4, 120, 160)]
            assert connection.exec_driver_sql(
                "SELECT run_id,models,tools,model_limit,tool_limit FROM runtime_automation_budgets "
                "ORDER BY run_id"
            ).all() == [(1, 17, 19, 120, 160), (2, 0, 0, 120, 160)]
            connection.exec_driver_sql("INSERT INTO runtime_work_budgets(root_id) VALUES ('new')")
            assert connection.exec_driver_sql(
                "SELECT model_limit,tool_limit FROM runtime_work_budgets WHERE root_id='new'"
            ).one() == (None, None)
            with pytest.raises(RuntimeError, match="compatible_rollback"):
                migration.downgrade()
            assert "model_limit" in {
                column["name"]
                for column in sa.inspect(connection).get_columns("runtime_automation_budgets")
            }


@pytest.mark.asyncio
async def test_automation_run_unlimited_count_and_explicit_limit():
    engine = create_async_engine("sqlite+aiosqlite://")
    try:
        async with engine.begin() as connection:
            await connection.exec_driver_sql("CREATE TABLE runtime_work (id TEXT PRIMARY KEY)")
            await connection.exec_driver_sql(
                "CREATE TABLE automation_runs (id INTEGER PRIMARY KEY)"
            )
            await connection.exec_driver_sql("INSERT INTO automation_runs VALUES (1)")
            await connection.run_sync(create_current_budget_tables)
        sessions = async_sessionmaker(engine)
        for _ in range(2):
            async with sessions() as session, session.begin():
                await charge_automation_run(session, 1, models=200, tools=200)
        async with sessions() as session, session.begin():
            row = (await session.execute(sa.select(automation_budgets))).mappings().one()
            assert (row["models"], row["tools"]) == (400, 400)
            assert row["model_limit"] is None and row["tool_limit"] is None
            await session.execute(
                sa.update(automation_budgets).values(model_limit=400, tool_limit=400)
            )
        with pytest.raises(WorkBudgetExceeded, match="automation_run"):
            async with sessions() as session, session.begin():
                await charge_automation_run(session, 1, models=1, tools=0)
        async with sessions() as session:
            assert await session.scalar(sa.select(automation_budgets.c.models)) == 400
    finally:
        await engine.dispose()
