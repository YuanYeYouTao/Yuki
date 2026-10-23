"""A worker uses the configuration at activation rather than fixed segment caps."""

import json

import pytest
from sqlalchemy import select
from tests.support.automation_unified_delivery_helpers import setup_run
from tests.support.runtime_execution import make_child_executor

from yuki.domain.messages import ChatResponse, ToolCall, ToolFunction
from yuki.runtime.work_repository import WorkRepository
from yuki.runtime.work_schema_v1 import work
from yuki.tool_results.artifacts import ToolArtifactRepository


@pytest.mark.parametrize("limit_kind,limit", [("model_requests", 2), ("tool_calls", 1)])
async def test_child_obeys_hot_limit_without_resetting_original_work(
    database, tmp_path, limit_kind, limit
):
    case = await setup_run(database, tmp_path, mode="silent")
    case.chat._tool_artifacts = ToolArtifactRepository(
        database, tmp_path / "artifacts", retention_seconds=60, max_artifact_bytes=100000
    )
    database.subagents_enabled = True
    worker_requests = 0

    def respond(request):
        nonlocal worker_requests
        if any(
            m.role == "user" and '"cwd": "/workspace/tasks/' in m.content for m in request.messages
        ):
            worker_requests += 1
            return ChatResponse(
                "",
                0,
                tool_calls=(
                    ToolCall(
                        f"read-{worker_requests}",
                        ToolFunction("query_chat_history", '{"view": "recent"}'),
                    ),
                ),
            )
        if len(case.provider.requests) == 1:
            return ChatResponse(
                "",
                0,
                tool_calls=(
                    ToolCall(
                        "accept", ToolFunction("task_control", '{"action":"accept","goal":"check"}')
                    ),
                    ToolCall(
                        "spawn", ToolFunction("subagent_start", '{"goal":"read original history"}')
                    ),
                ),
            )
        return "Parent retains the independent worker."

    case.provider._responder = respond
    case.clock.advance(2)
    await case.dispatch()
    async with database.read_session() as reader:
        child = next(
            dict(row)
            for row in (await reader.execute(select(work))).mappings()
            if json.loads(row["source_json"]).get("worker")
        )
    await case.chat._runtime_config.set_override(
        f"agent.max_{limit_kind}",
        limit,
        scope_type="global",
        scope_id="",
        actor_user_id="10001",
        trigger_message_id="hot-update",
        conversation_key="test",
    )
    repository = WorkRepository(database)
    executor = make_child_executor(
        repository,
        chat=case.chat,
        config=case.chat._runtime_config,
        runner=case.chat.runtime.runner,
        load_tools=case.chat.runtime.runner.main_contract.definitions,
    )
    await executor.run(child["id"])
    retained = await repository.get(child["id"])
    assert retained[limit_kind] == limit
    assert retained["state"] == "queued"
    assert retained["source_json"] == child["source_json"]
    assert retained["id"] == child["id"]
    if limit_kind == "model_requests":
        assert worker_requests == limit
