"""Persisted Work lookup remains usable from an unregistered activation."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import event
from tests.conftest import build_harness, make_settings
from tests.support.social_identity_cases import social_env

from qq_ai_bot.domain.messages import ChatMessage, ToolCall, ToolFunction
from qq_ai_bot.runtime.origin import TurnOrigin
from qq_ai_bot.runtime.subagent_repository import SubagentRepository
from qq_ai_bot.runtime.work_control import WorkControl
from qq_ai_bot.runtime.work_repository import WorkConflict, WorkRepository
from qq_ai_bot.runtime.work_wait import WorkWaitRepository
from qq_ai_bot.services.agent_runner import AgentRunResult, AgentRuntime
from qq_ai_bot.services.main_agent_turns import MainAgentTurnService


async def completed_work(database, tmp_path):
    env = await social_env(database, tmp_path)
    repository = WorkRepository(database)
    lease = await repository.acquire(env.context.conversation_id, 1)
    assert lease is not None
    source = {"actor_user_id": "10001", "origin": "user_message"}
    row = await repository.accept(
        lease, source_key="original-request", source=source, goal="test the runtime"
    )
    row = await repository.transition(lease, row["id"], row["revision"], "completed")
    validate = AsyncMock()
    control = WorkControl(repository, lease, "status-question", source, validate)
    return control, row, validate


@pytest.mark.asyncio
async def test_terminal_work_queries_do_not_register_resume_or_write(database, tmp_path):
    control, row, validate = await completed_work(database, tmp_path)
    statements = []

    def record_statement(_conn, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement.strip().upper())

    event.listen(database.engine.sync_engine, "before_cursor_execute", record_statement)
    try:
        fetched = json.loads(
            await control.execute("task_control", {"action": "get", "work_id": row["id"]}, "get")
        )
        listed = json.loads(
            await control.execute(
                "task_control", {"action": "list", "limit": 1, "status": "terminal"}, "list"
            )
        )
    finally:
        event.remove(database.engine.sync_engine, "before_cursor_execute", record_statement)
    assert fetched["ok"] and fetched["work"]["state"] == "completed"
    assert fetched["work"]["work_id"] == row["id"]
    assert listed["works"] == [fetched["work"]]
    assert validate.await_count == 2
    assert not any(
        sql.startswith(("BEGIN IMMEDIATE", "INSERT", "UPDATE", "DELETE")) for sql in statements
    )
    assert control.current is None and control.handoff_work_id is None and control.ending is None
    assert await control.repository.get(row["id"]) == row


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [0, 51, True, "1"])
async def test_query_limit_is_validated_before_repository_read(database, tmp_path, limit):
    control, _, _ = await completed_work(database, tmp_path)
    response = json.loads(
        await control.execute("task_control", {"action": "list", "limit": limit}, "invalid")
    )
    assert response == {"ok": False, "error": "work_list_limit_invalid"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arguments", "error"),
    [
        ({"status": []}, "work_list_status_invalid"),
        ({"cursor": 1}, "work_list_cursor_invalid"),
    ],
)
async def test_query_options_reject_untyped_arguments(database, tmp_path, arguments, error):
    control, _, _ = await completed_work(database, tmp_path)
    response = json.loads(
        await control.execute("task_control", {"action": "list", **arguments}, "invalid")
    )
    assert response == {"ok": False, "error": error}


@pytest.mark.asyncio
async def test_status_question_request_contains_terminal_work_and_preserves_prefix(
    database, tmp_path
):
    control, row, _ = await completed_work(database, tmp_path)
    chat = build_harness(database, make_settings(database.url)).processor._chat
    runtime = AgentRuntime(
        origin=TurnOrigin.USER_MESSAGE,
        actor_user_id="10001",
        actor_is_superuser=False,
        delegated_authority=None,
        conversation_key="status-question",
        current_group_id=None,
        bot_user_id="7777",
        gateway=None,
        runtime_config=await chat._runtime_config.snapshot(),
        current_time=chat._time.current_default(),
        allowed_capabilities=frozenset(),
        max_tool_calls=8,
        max_model_requests=8,
        work_control=control,
    )
    result = AgentRunResult("already completed", 0, 1, False)
    runner = SimpleNamespace(run=AsyncMock(return_value=result))
    turns = MainAgentTurnService(chat.runtime.main_turns._composer, runner)
    original = (ChatMessage("system", "fixed contract"), ChatMessage("user", "什么时候结束？"))
    assert await turns._run_prepared(original, runtime, None) is result
    submitted, submitted_runtime, _ = runner.run.call_args.args
    assert submitted[: len(original)] == original
    assert submitted == original
    state = await control.runtime_state()
    assert state["state"] == "no_active_work"
    assert state["state_scope"] == "current_activation"
    assert "work_id" not in state and "goal" not in state and "reporting" not in state
    assert state["recent_work"]["work_id"] == row["id"]
    assert set(state["recent_work"]) == {
        "work_id",
        "goal_excerpt",
        "state",
    }
    detailed = json.loads(
        await control.execute("task_control", {"action": "get", "work_id": row["id"]}, "details")
    )["work"]
    assert detailed["conversation_id"] == row["conversation_id"]
    assert detailed["generation"] == row["generation"]
    assert "created_at" in detailed and "updated_at" in detailed
    assert state["recent_work"]["state"] == "completed"
    assert "available_work" not in state
    assert submitted_runtime.dynamic_context_prepared
    assert control.current_message is original[-1]
    assert control.current is None


@pytest.mark.asyncio
async def test_idle_work_directory_is_bounded_and_full_goal_wait_are_queryable(database, tmp_path):
    env = await social_env(database, tmp_path)
    repository = WorkRepository(database)
    lease = await repository.acquire(env.context.conversation_id, 1)
    assert lease is not None
    source = {
        "origin": "user_message",
        "principal_kind": "person",
        "actor_user_id": "10001",
        "actor_person_id": env.person,
    }
    goal = "完整约束" * 2046 + "最终禁止重复执行"
    rows = [
        await repository.accept(lease, source_key=f"large-{index}", source=source, goal=goal)
        for index in range(16)
    ]
    target = rows[-1]
    waiting = WorkWaitRepository(repository)
    await waiting.register(
        lease,
        work_id=target["id"],
        source=source,
        call_key="large-wait",
        mode="all",
        conditions=[{"kind": "time_due", "after_seconds": 3600}, {"kind": "conversation"}],
        deadline_at=None,
    )
    await repository.transition(lease, target["id"], target["revision"], "waiting_external")
    control = WorkControl(repository, lease, "directory-question", source, AsyncMock())
    statements = []

    def record_statement(_conn, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement.lower())

    event.listen(database.engine.sync_engine, "before_cursor_execute", record_statement)
    try:
        state = await control.runtime_state()
    finally:
        event.remove(database.engine.sync_engine, "before_cursor_execute", record_statement)
    assert len(state["available_work"]) == 16
    assert len(json.dumps(state, ensure_ascii=False)) < 9000
    assert all("conditions_json" not in statement for statement in statements)
    for item in (*state["available_work"], state["recent_work"]):
        assert "goal" not in item and "wait" not in item
        assert item["goal_excerpt"] == goal[:160] and not item["goal_complete"]
    assert next(item for item in state["available_work"] if item["work_id"] == target["id"])[
        "has_wait"
    ]
    detail = json.loads(
        await control.execute("task_control", {"action": "get", "work_id": target["id"]}, "full")
    )["work"]
    assert detail["goal"] == goal
    assert detail["wait"] == await waiting.describe(target["id"])
    assert detail["wait"]["status"] == "active"
    assert (await repository.get(target["id"]))["state"] == "waiting_external"
    assert control.current is None and control.handoff_work_id is None
    listed = json.loads(
        await control.execute("task_control", {"action": "list", "limit": 16}, "full-list")
    )
    assert all(item["goal"] == goal for item in listed["works"])


@pytest.mark.asyncio
async def test_current_work_prompt_preserves_full_goal_and_does_not_load_recent(database, tmp_path):
    control, _, _ = await completed_work(database, tmp_path)
    goal = "保持原目标和最后的限制" * 500
    current = await control.repository.accept(
        control.lease, source_key="continued-work", source=control.source, goal=goal
    )
    control.current = current
    state = await control.runtime_state()
    assert state["work_id"] == current["id"]
    assert state["goal"] == goal
    assert state["state"] == current["state"]
    assert "recent_work" not in state and "available_work" not in state


@pytest.mark.asyncio
async def test_worker_can_query_own_work_but_not_root_or_obsolete_parent(database, tmp_path):
    env = await social_env(database, tmp_path)
    repository = WorkRepository(database)
    parent_lease = await repository.acquire(env.context.conversation_id, 1)
    assert parent_lease is not None
    source = {"actor_user_id": "10001", "origin": "user_message"}
    parent = await repository.accept(
        parent_lease, source_key="parent", source=source, goal="investigate"
    )
    children = SubagentRepository(repository)
    identity = await children.start(
        parent_lease, parent["id"], "child", {"goal": "read facts", "output_kind": "answer"}
    )
    lease = await children.acquire(identity)
    assert lease is not None
    child = await repository.get(identity)

    async def validate():
        if (await repository.get(parent["id"]))["state"] != "running":
            raise WorkConflict("worker_parent_obsolete")

    control = WorkControl(repository, lease, "child", json.loads(child["source_json"]), validate)
    control.current = child
    sibling = await children.start(
        parent_lease, parent["id"], "sibling", {"goal": "other facts", "output_kind": "answer"}
    )
    chat = build_harness(database, make_settings(database.url)).processor._chat
    from qq_ai_bot.runtime.work_activation import current_work_control
    from qq_ai_bot.services.agent_tools import ToolRuntime
    from qq_ai_bot.services.main_agent_backend import MainAgentBackend

    # The real backend without Automation authority: a child lease reads its own
    # lifecycle through WorkQueries' ownership fence, never the root directory.
    tool_runtime = ToolRuntime(inbound=None, gateway=None, allow_generic_onebot=False)
    backend = MainAgentBackend(chat, tool_runtime, allowed_tools=frozenset({"task_control"}))
    without = MainAgentBackend(chat, tool_runtime, allowed_tools=frozenset({"search_memory"}))
    token = current_work_control.set(control)
    try:
        assert backend.work_query_allowed("get") and backend.work_query_allowed("list")
        assert not without.work_query_allowed("get")
    finally:
        current_work_control.reset(token)

    async def query(work_id):
        token = current_work_control.set(control)
        try:
            return await _query(work_id)
        finally:
            current_work_control.reset(token)

    async def _query(work_id):
        result = await chat.runtime.runner._execute_tool_batch(
            (
                ToolCall(
                    "lookup",
                    ToolFunction("task_control", json.dumps({"action": "get", "work_id": work_id})),
                ),
            ),
            backend,
            SimpleNamespace(work_control=control),
            remaining_calls=8,
            max_parallel_calls=1,
            reusable_results={},
            cacheable_names=frozenset(),
            declared_names=frozenset({"task_control"}),
        )
        return json.loads(result.calls[0][1])

    assert (await query(identity))["work"]["work_id"] == identity
    for other in (parent["id"], sibling):
        assert await query(other) == {
            "ok": False,
            "error": "work_not_found_or_not_authorized",
        }
    await repository.transition(parent_lease, parent["id"], parent["revision"], "completed")
    assert await query(identity) == {"ok": False, "error": "worker_parent_obsolete"}
