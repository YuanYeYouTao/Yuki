"""Scheduled creators retain their real run through the shared child executor."""

import json
from uuid import uuid4

import pytest
from sqlalchemy import select, update
from tests.support.automation_unified_delivery_helpers import setup_run
from tests.support.runtime_execution import make_child_executor

from qq_ai_bot.automation.models import RunStatus
from qq_ai_bot.domain.messages import ChatResponse, ToolCall, ToolFunction
from qq_ai_bot.identity.db_models import IdentityBindingModel, SpaceBindingModel
from qq_ai_bot.runtime.work_repository import WorkRepository
from qq_ai_bot.runtime.work_schema_v1 import work
from qq_ai_bot.runtime.work_wait import WorkWaitRepository
from qq_ai_bot.services.execution_sources import recover_execution_source
from qq_ai_bot.tool_results.artifacts import ToolArtifactRepository

UPDATED_GOAL = "Check original scheduled history"


@pytest.mark.parametrize("principal", ["self", "person"])
async def test_scheduled_child_runs_without_a_fabricated_message_or_initiative(
    database, tmp_path, principal
):
    case = await setup_run(database, tmp_path, delivery="none", principal=principal)
    case.chat._tool_artifacts = ToolArtifactRepository(
        database, tmp_path / "artifacts", retention_seconds=60, max_artifact_bytes=100000
    )
    database.subagents_enabled = True
    parent_requests = 0
    worker_requests = 0
    worker_results = []

    def respond(request):
        nonlocal parent_requests, worker_requests
        if "你是 Yuki 派出的持久工作者" in request.messages[0].content:
            worker_requests += 1
            if worker_requests == 1:
                return ChatResponse(
                    "",
                    0,
                    tool_calls=(
                        ToolCall(
                            "revise-original-brief",
                            ToolFunction(
                                "task_control",
                                json.dumps(
                                    {
                                        "action": "update",
                                        "goal": UPDATED_GOAL,
                                    }
                                ),
                            ),
                        ),
                    ),
                )
            if worker_requests == 2:
                return ChatResponse(
                    "",
                    0,
                    tool_calls=(
                        ToolCall(
                            "read-original-history",
                            ToolFunction("get_recent_chat_history", "{}"),
                        ),
                    ),
                )
            result = next(
                json.loads(message.content)
                for message in reversed(request.messages)
                if message.role == "tool"
            )
            worker_results.append(result)
            return "Worker verified the original scheduled task."
        parent_requests += 1
        if parent_requests == 1:
            call = ToolCall(
                "spawn-original",
                ToolFunction(
                    "subagent_start",
                    json.dumps(
                        {
                            "goal": "Check the scheduled task",
                            "acceptance": "Return the checked result",
                            "output_kind": "answer",
                        }
                    ),
                ),
            )
        elif parent_requests == 2:
            result = next(
                json.loads(message.content)
                for message in reversed(request.messages)
                if message.role == "tool"
            )
            call = ToolCall(
                "wait-original",
                ToolFunction(
                    "task_control",
                    json.dumps(
                        {
                            "action": "wait",
                            "conditions": [{"kind": "owned_run", "run_id": result["child_id"]}],
                        }
                    ),
                ),
            )
        else:
            return "Parent verified the worker result."
        return ChatResponse("", 0, tool_calls=(call,))

    case.provider._responder = respond
    first = await case.executor.execute(case.row, case.run)
    assert first.status is RunStatus.RUNNING
    async with database.sessions() as session:
        rows = [dict(row) for row in (await session.execute(select(work))).mappings()]
    parent = next(
        row
        for row in rows
        if json.loads(row["source_json"]).get("owner") == "automation"
        and not json.loads(row["source_json"]).get("worker")
    )
    child = next(row for row in rows if json.loads(row["source_json"]).get("worker"))
    source = json.loads(child["source_json"])
    assert source["automation_run_id"] == case.run.id
    assert source["origin"] == "scheduled_automation" and not source.get("trigger_event_id")
    assert not source.get("initiative_run_id")
    if principal == "person":
        await case.env.router.cas_takeover_person(case.env.person)
    async with database.sessions() as session, session.begin():
        session.add(
            SpaceBindingModel(
                id=str(uuid4()),
                space_id=source["space_id"],
                platform="qq",
                external_space_id="other-projection",
                status="active",
                first_seen_at=case.clock.now(),
                last_seen_at=case.clock.now(),
                created_at=case.clock.now(),
                updated_at=case.clock.now(),
            )
        )
        if principal == "person":
            session.add(
                IdentityBindingModel(
                    id=str(uuid4()),
                    person_id=case.env.person,
                    platform="qq",
                    external_account_id="10099",
                    status="active",
                    first_seen_at=case.clock.now(),
                    last_seen_at=case.clock.now(),
                    created_at=case.clock.now(),
                    updated_at=case.clock.now(),
                )
            )
    await recover_execution_source(
        database,
        child["conversation_id"],
        source,
        request_id=child["id"],
        settings=case.executor._settings,
    )
    if principal == "person":
        recovered = await recover_execution_source(
            database,
            child["conversation_id"],
            {**source, "actor_user_id": "old-derived-account"},
            request_id=child["id"],
            settings=case.executor._settings,
        )
        assert recovered.actor_person_id == source["actor_person_id"]
        assert recovered.actor_user_id == "10001"
    for changed in (
        {"generation": source["generation"] + 1},
        {"automation_run_id": case.run.id + 1},
        {"actor_person_id": "other-canonical-owner"},
        {"parent_execution_id": "other-execution"},
    ):
        with pytest.raises(ValueError, match="automation_task_source_changed"):
            await recover_execution_source(
                database,
                child["conversation_id"],
                {**source, **changed},
                request_id=child["id"],
                settings=case.executor._settings,
            )
    async with database.sessions() as session, session.begin():
        await session.execute(
            update(SpaceBindingModel)
            .where(
                SpaceBindingModel.space_id == source["space_id"],
                SpaceBindingModel.external_space_id == source["current_group_id"],
            )
            .values(status="disabled")
        )
    with pytest.raises(ValueError, match="task_space_binding_unavailable"):
        await recover_execution_source(
            database,
            child["conversation_id"],
            source,
            request_id=child["id"],
            settings=case.executor._settings,
        )
    async with database.sessions() as session, session.begin():
        await session.execute(
            update(SpaceBindingModel)
            .where(
                SpaceBindingModel.space_id == source["space_id"],
                SpaceBindingModel.external_space_id == source["current_group_id"],
            )
            .values(status="active")
        )
    repository = WorkRepository(database)
    executor = make_child_executor(
        repository,
        chat=case.chat,
        config=case.chat._runtime_config,
        runner=case.chat.runtime.runner,
        load_tools=case.chat.runtime.runner.main_contract.definitions,
    )
    assert await executor.run(child["id"]) is None
    assert worker_results[0]["ok"], worker_results[0]
    completed = await repository.get(child["id"])
    assert completed["state"] == "completed" and completed["model_requests"] == 3
    assert completed["goal"] == UPDATED_GOAL
    assert json.loads(completed["checkpoint_json"])["sync_result"].startswith("Worker verified")
    assert json.loads(completed["source_json"]) == source
    await WorkWaitRepository(repository).deliver_due()
    result = await case.executor.execute(case.row, case.run)
    assert result.status is RunStatus.SUCCEEDED
    assert (await repository.get(parent["id"]))["state"] == "completed"
    assert case.run.id == source["automation_run_id"]
