"""Compact runtime hints retain scoped task selection and execution facts."""

import json
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import event
from tests.support.social_identity_cases import social_env

from qq_ai_bot.runtime.work_control import WorkControl
from qq_ai_bot.runtime.work_repository import WorkRepository


async def control_for(database, tmp_path):
    env = await social_env(database, tmp_path)
    repository = WorkRepository(database)
    lease = await repository.acquire(env.context.conversation_id, 1)
    assert lease is not None
    source = {"actor_user_id": "10001", "origin": "user_message"}
    return WorkControl(repository, lease, "status", source, AsyncMock())


async def test_empty_activation_has_only_local_state_marker(database, tmp_path):
    control = await control_for(database, tmp_path)
    assert await control.runtime_state() == {
        "state": "no_active_work",
        "state_scope": "current_activation",
    }


@pytest.mark.parametrize("goal", ["完成 HTML 页面", "保留完整原目标" * 40])
async def test_recent_hint_is_small_read_only_and_full_details_remain_queryable(
    database, tmp_path, goal
):
    control = await control_for(database, tmp_path)
    row = await control.repository.accept(
        control.lease, source_key="request", source=control.source, goal=goal
    )
    row = await control.repository.transition(
        control.lease, row["id"], row["revision"], "completed"
    )
    statements = []

    def record(_conn, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement.lower())

    event.listen(database.engine.sync_engine, "before_cursor_execute", record)
    try:
        state = await control.runtime_state()
    finally:
        event.remove(database.engine.sync_engine, "before_cursor_execute", record)
    recent = {"work_id": row["id"], "state": "completed", "goal_excerpt": goal[:160]}
    if len(goal) > 160:
        recent["goal_complete"] = False
    assert state == {
        "state": "no_active_work",
        "state_scope": "current_activation",
        "recent_work": recent,
    }
    assert not any(sql.lstrip().startswith(("insert", "update", "delete")) for sql in statements)
    assert all("model_requests" not in sql for sql in statements)
    detail = json.loads(
        await control.execute("task_control", {"action": "get", "work_id": row["id"]}, "get")
    )["work"]
    assert detail["goal"] == goal
    for key in ("model_requests", "tool_calls", "sent_messages"):
        assert detail[key] == row[key]
    assert await control.repository.get(row["id"]) == row

    other_source = {**control.source, "actor_user_id": "10002"}
    other = WorkControl(control.repository, control.lease, "other", other_source, AsyncMock())
    assert await other.runtime_state() == {
        "state": "no_active_work",
        "state_scope": "current_activation",
    }


@pytest.mark.parametrize("reporting", [None, "interactive", "quiet"])
async def test_active_work_keeps_full_goal_state_and_reporting(database, tmp_path, reporting):
    control = await control_for(database, tmp_path)
    goal = "保持执行目标和原约束" * 100
    row = await control.repository.accept(
        control.lease, source_key="active", source=control.source, goal=goal, reporting=reporting
    )
    control.current = row
    expected = {
        "state": row["state"],
        "state_scope": "current_activation",
        "work_id": row["id"],
        "goal": goal,
    }
    if reporting is not None:
        expected["reporting"] = reporting
    assert await control.runtime_state() == expected
    assert await control.repository.get(row["id"]) == row
