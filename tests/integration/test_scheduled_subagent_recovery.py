"""Scheduled creators retain their real run through the shared child executor."""

import asyncio
import json
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import select, update
from tests.support.automation_unified_delivery_helpers import setup_run
from tests.support.runtime_execution import make_child_executor

from yuki.automation.models import RunStatus
from yuki.domain.messages import ChatResponse, ToolCall, ToolFunction
from yuki.identity.canonical_repository import ensure_account, replace_assignment
from yuki.identity.db_models import CanonicalPersonModel, SpaceBindingModel
from yuki.runtime.subagent_repository import SubagentRepository
from yuki.runtime.subagent_scheduler import SubagentScheduler
from yuki.runtime.work_repository import WorkRepository
from yuki.runtime.work_schema_v1 import work
from yuki.runtime.work_wait import WorkWaitRepository
from yuki.services.execution_sources import recover_execution_source
from yuki.tool_results.artifacts import ToolArtifactRepository

UPDATED_GOAL = "Check original scheduled history"


@pytest.mark.parametrize("principal", ["self", "person"])
async def test_scheduled_child_runs_without_a_fabricated_message_or_initiative(
    database, tmp_path, principal, monkeypatch
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
        if any(
            m.role == "user" and '"cwd": "/workspace/tasks/' in m.content for m in request.messages
        ):
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
                            ToolFunction("query_chat_history", '{"view": "recent"}'),
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
        accept = ToolCall(
            "accept-original",
            ToolFunction(
                "task_control", '{"action":"accept","goal":"Check the original scheduled task"}'
            ),
        )
        return ChatResponse("", 0, tool_calls=(accept, call) if parent_requests == 1 else (call,))

    case.provider._responder = respond
    case.clock.advance(2)
    first = await case.dispatch()
    assert first.status is RunStatus.RUNNING
    async with database.read_session() as session:
        rows = [dict(row) for row in (await session.execute(select(work))).mappings()]
    parent = next(
        row
        for row in rows
        if json.loads(row["source_json"]).get("owner") == "automation"
        and not json.loads(row["source_json"]).get("worker")
    )
    child = next(row for row in rows if json.loads(row["source_json"]).get("worker"))
    source = json.loads(child["source_json"])
    assert child["parent_work_id"] == parent["id"]
    assert source == {
        **json.loads(parent["source_json"]),
        "work_id": child["id"],
        "parent_work_id": parent["id"],
        "worker": True,
    }
    assert source["automation_run_id"] == case.run.id
    assert source["origin"] == "scheduled_automation" and not source.get("trigger_event_id")
    assert not source.get("initiative_run_id")
    if principal == "person":
        assert source["actor_person_id"] == case.env.person
        await case.env.router.cas_takeover_person(case.env.person)
    async with database.immediate_session() as session:
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
            person = await session.get(CanonicalPersonModel, case.env.person)
            assert person is not None
            account = await ensure_account(session, "10099", platform="qq", now=case.clock.now())
            await replace_assignment(session, account, None, person, now=case.clock.now())
    await recover_execution_source(
        database,
        child["conversation_id"],
        source,
        request_id=child["id"],
    )
    if principal == "person":
        recovered = await recover_execution_source(
            database,
            child["conversation_id"],
            {**source, "actor_user_id": "old-derived-account"},
            request_id=child["id"],
        )
        assert recovered.actor_person_id == source["actor_person_id"]
        assert recovered.actor_user_id == "old-derived-account"
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
            )
    async with database.immediate_session() as session:
        await session.execute(
            update(SpaceBindingModel)
            .where(
                SpaceBindingModel.space_id == source["space_id"],
                SpaceBindingModel.external_space_id == source["current_group_id"],
            )
            .values(status="disabled")
        )
    recovered = await recover_execution_source(
        database,
        child["conversation_id"],
        source,
        request_id=child["id"],
    )
    assert recovered.generation == source["generation"]
    assert recovered.presence_id == source["presence_id"]
    async with database.immediate_session() as session:
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
    maintenance = AsyncMock(side_effect=ValueError("original maintenance unavailable"))
    repeated_failure = asyncio.Event()
    completed_worker = asyncio.Event()
    run_worker = executor.run

    async def cancellation_unavailable():
        if maintenance.await_count >= 2:
            repeated_failure.set()
        raise OSError("original cancellation unavailable")

    async def observed_worker(identity):
        result = await run_worker(identity)
        completed_worker.set()
        return result

    cancellation = AsyncMock(side_effect=cancellation_unavailable)
    monkeypatch.setattr(executor.children, "maintain", maintenance)
    monkeypatch.setattr(executor, "cancel_commands", cancellation)
    monkeypatch.setattr(executor, "run", observed_worker)
    scheduler = SubagentScheduler(repository, executor.children, executor, admission_enabled=True)
    await scheduler.start()
    try:
        await asyncio.wait_for(
            asyncio.gather(completed_worker.wait(), repeated_failure.wait()), timeout=5
        )
    finally:
        await scheduler.close()
    assert worker_results[0]["ok"], worker_results[0]
    completed = await repository.get(child["id"])
    assert completed["state"] == "completed" and completed["model_requests"] == 3
    assert completed["goal"] == UPDATED_GOAL
    assert json.loads(completed["checkpoint_json"])["sync_result"].startswith("Worker verified")
    assert json.loads(completed["source_json"]) == source
    await WorkWaitRepository(repository).deliver_due()
    from tests.support.runtime_execution import make_work_resumer

    from yuki.runtime.work_scheduler import WorkScheduler

    resumer = make_work_resumer(
        repository,
        ledger=case.chat._ledger,
        scopes=case.chat._conversation_scopes,
        turns=case.chat._turn_coordinator,
        router=case.env.router,
        config=case.chat._runtime_config,
        generate_self=case.chat.generate_self_initiative,
        generate_wakeup=case.chat.generate_main_agent_wakeup,
        bindings=case.chat.runtime.bindings,
        settings=case.settings,
        generate_origin=case.chat.generate_origin_turn,
    )
    await WorkScheduler(repository, resumer.resume, chat_admission_enabled=True).drain_once()
    result = (await case.repository.run_history(case.row.id))[0]
    assert result.status is RunStatus.SUCCEEDED
    assert (await repository.get(parent["id"]))["state"] == "completed"
    assert case.run.id == source["automation_run_id"]
    original_parent = await repository.get(parent["id"])
    lease = await repository.acquire(parent["conversation_id"], parent["generation"])
    workers = SubagentRepository(repository)
    await workers.resume(lease, parent["id"], child["id"], key="continue-retained-child")
    await repository.release(lease)
    case.provider._responder = lambda _: "Worker continued the original scheduled source."
    assert await executor.run(child["id"]) is None
    continued = await repository.get(child["id"])
    assert continued["state"] == "completed" and continued["model_requests"] == 4
    assert json.loads(continued["source_json"]) == source
    unchanged_parent = await repository.get(parent["id"])
    assert unchanged_parent["state"] == "completed"
    assert unchanged_parent["model_requests"] == original_parent["model_requests"]
    from yuki.persistence.models import AutomationModel, AutomationRunModel

    async with database.read_session() as session:
        assert (await session.get(AutomationRunModel, case.run.id)).status == "succeeded"
        assert (await session.get(AutomationModel, case.row.id)).status == "completed"


async def test_self_child_runs_and_continues_after_parent_and_original_initiative_settle(
    database, tmp_path
):
    from tests.conftest import build_harness, make_settings
    from tests.support.fixed_contract_fixture import bind_main_contract
    from tests.unit.test_self_initiative_runtime import self_source

    from yuki.conversation.autonomy_db_models import InitiativeRunModel
    from yuki.llm.fake import FakeLLMProvider
    from yuki.runtime.work_budget_schema import budgets

    source, admissions, _ = await self_source(database)

    def respond(_request):
        if len(provider.requests) == 1:
            return ChatResponse(
                "",
                0,
                tool_calls=(
                    ToolCall(
                        "derive-original-worker",
                        ToolFunction(
                            "task_control",
                            json.dumps({"action": "derive", "goal": "check the nested SELF fact"}),
                        ),
                    ),
                ),
            )
        return "SELF child checked the actual source."

    provider = FakeLLMProvider(respond)
    harness = build_harness(
        database,
        make_settings(database.url, runtime_work_enabled=True, enabled_groups_csv="2001"),
        provider,
    )
    bind_main_contract(harness, tmp_path)
    chat = harness.processor._chat
    chat._tool_artifacts = ToolArtifactRepository(
        database, tmp_path / "artifacts", retention_seconds=60, max_artifact_bytes=100000
    )
    repository = WorkRepository(database)
    lease = await repository.acquire(source["conversation_id"], source["generation"])
    parent = await repository.accept(
        lease,
        source_key=f"initiative:{source['initiative_run_id']}",
        source=source,
        goal="inspect the original SELF scene",
    )
    database.subagents_enabled = True
    workers = SubagentRepository(repository)
    identity = await workers.start(
        lease, parent["id"], "self-child", {"goal": "check independently"}
    )
    await repository.accept_control(
        lease, parent["id"], {"action": "complete", "call_key": "parent-done", "result": "retained"}
    )
    current = await repository.get(parent["id"])
    await repository.transition(lease, parent["id"], current["revision"], "completed")
    await admissions.record_feedback(source["initiative_run_id"], sequence=1, outcome="no_reply")
    await repository.release(lease)
    executor = make_child_executor(
        repository,
        chat=chat,
        config=chat._runtime_config,
        runner=chat.runtime.runner,
        load_tools=chat.runtime.runner.main_contract.definitions,
    )
    assert await executor.run(identity) is None
    before = await repository.get(identity)
    assert before["state"] == "completed" and before["model_requests"] == 2
    async with database.read_session() as session:
        nested = (
            (await session.execute(select(work).where(work.c.parent_work_id == identity)))
            .mappings()
            .one()
        )
    assert nested["state"] == "queued"
    nested_source = json.loads(nested["source_json"])
    assert nested_source["worker"] is True and nested_source["work_id"] == nested["id"]
    assert nested_source["initiative_run_id"] == source["initiative_run_id"]
    assert await executor.run(nested["id"]) is None
    assert (await repository.get(nested["id"]))["state"] == "completed"
    assert (await repository.get(nested["id"]))["model_requests"] == 1
    worker_tools = {tool.name for tool in provider.requests[0].tools}
    assert worker_tools == {tool.name for tool in provider.requests[2].tools}
    assert {"task_control", "subagent_start", "send"} <= worker_tools
    assert provider.requests[0].tools == await chat.runtime.runner.main_contract.model_definitions()
    assert provider.requests[0].messages[0] == chat._prompt_composer.static_messages()[0]
    lease = await repository.acquire(source["conversation_id"], source["generation"])
    await workers.resume(lease, parent["id"], identity, key="continue-self-child")
    await repository.release(lease)
    assert await executor.run(identity) is None
    after = await repository.get(identity)
    assert after["state"] == "completed" and after["model_requests"] == 3
    assert after["source_json"] == before["source_json"]
    assert (await repository.get(parent["id"]))["state"] == "completed"
    assert (await repository.get(parent["id"]))["model_requests"] == 0
    assert len(provider.requests) == 4
    async with database.read_session() as session:
        assert (
            await session.get(InitiativeRunModel, source["initiative_run_id"])
        ).state == "no_reply"
        budget = (
            (await session.execute(select(budgets).where(budgets.c.root_id == parent["id"])))
            .mappings()
            .one()
        )
        assert budget["models"] == 4


async def test_expired_worker_does_not_repurchase_response_lost_with_process(database, tmp_path):
    import asyncio
    import sys
    import time
    from textwrap import dedent

    from tests.conftest import build_harness, make_settings
    from tests.support.fixed_contract_fixture import bind_main_contract
    from tests.unit.test_self_initiative_runtime import self_source

    from yuki.llm.fake import FakeLLMProvider
    from yuki.runtime.subagent_schema import children
    from yuki.runtime.work_budget_schema import budgets
    from yuki.runtime.work_recovery_schema import recovery
    from yuki.runtime.work_schema_v1 import journal

    source, _admissions, _binding = await self_source(database)
    repository = WorkRepository(database)
    lease = await repository.acquire(source["conversation_id"], source["generation"])
    root = await repository.accept(
        lease, source_key=f"initiative:{source['initiative_run_id']}", source=source, goal="inspect"
    )
    database.subagents_enabled = True
    workers = SubagentRepository(repository)
    identity = await workers.start(
        lease, root["id"], "paid-child", {"goal": "inspect independently"}
    )
    await repository.release(lease)
    issued = tmp_path / "actual-request.txt"
    script = dedent("""
        import asyncio
        import os
        import sys
        from pathlib import Path
        from tests.conftest import build_harness, make_settings
        from tests.support.fixed_contract_fixture import bind_main_contract
        from tests.support.runtime_execution import make_child_executor
        from yuki.domain.messages import ChatResponse
        from yuki.llm.fake import FakeLLMProvider
        from yuki.persistence.database import Database
        from yuki.runtime.work_repository import WorkRepository
        from yuki.tool_results.artifacts import ToolArtifactRepository

        async def run():
            database = Database(sys.argv[1])
            def respond(_request):
                response = ChatResponse('Original response lost before publication.', 0)
                Path(sys.argv[3]).write_text(response.content, encoding='utf-8')
                os._exit(23)
            harness = build_harness(database,
                make_settings(database.url, runtime_work_enabled=True, enabled_groups_csv='2001'),
                FakeLLMProvider(respond))
            bind_main_contract(harness, Path(sys.argv[4]))
            chat = harness.processor._chat
            chat._tool_artifacts = ToolArtifactRepository(database, Path(sys.argv[4]) / 'artifacts',
                retention_seconds=60, max_artifact_bytes=100000)
            executor = make_child_executor(WorkRepository(database), chat=chat,
                config=chat._runtime_config, runner=chat.runtime.runner,
                load_tools=chat.runtime.runner.main_contract.definitions)
            await executor.run(sys.argv[2])
        asyncio.run(run())
    """)
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        script,
        database.url,
        identity,
        str(issued),
        str(tmp_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    output, error = await asyncio.wait_for(process.communicate(), timeout=30)
    assert process.returncode == 23, (output.decode(), error.decode())
    assert issued.read_text(encoding="utf-8") == "Original response lost before publication."
    before = await repository.get(identity)
    assert before["state"] == "running" and before["model_requests"] == 1
    async with database.read_session() as session:
        assert (
            await session.scalar(select(journal.c.phase).where(journal.c.work_id == identity))
            == "dispatched"
        )
        assert (
            await session.scalar(select(recovery.c.work_id).where(recovery.c.work_id == identity))
            is None
        )
    async with database.immediate_session() as writer:
        await writer.execute(
            update(children)
            .where(children.c.work_id == identity)
            .values(lease_until=time.time() - 1)
        )
    provider = FakeLLMProvider(lambda _: "Must not purchase the lost original request again.")
    harness = build_harness(
        database,
        make_settings(database.url, runtime_work_enabled=True, enabled_groups_csv="2001"),
        provider,
    )
    bind_main_contract(harness, tmp_path)
    chat = harness.processor._chat
    chat._tool_artifacts = ToolArtifactRepository(
        database, tmp_path / "artifacts", retention_seconds=60, max_artifact_bytes=100000
    )
    executor = make_child_executor(
        repository,
        chat=chat,
        config=chat._runtime_config,
        runner=chat.runtime.runner,
        load_tools=chat.runtime.runner.main_contract.definitions,
    )
    assert await executor.run(identity) == "work_response_not_persisted"
    after = await repository.get(identity)
    assert after["state"] == "failed" and after["reason"] == "work_response_not_persisted"
    assert after["source_json"] == before["source_json"]
    assert after["model_requests"] == 1 and after["tool_calls"] == 0
    assert not provider.requests
    async with database.read_session() as session:
        assert (
            await session.scalar(select(budgets.c.models).where(budgets.c.root_id == root["id"]))
            == 1
        )


@pytest.mark.parametrize("tool_name", ["query_chat_history", "update_short_state"])
async def test_actual_worker_tool_continues_after_nested_result_arrives(
    database, tmp_path, tool_name
):
    from tests.conftest import build_harness, make_settings
    from tests.support.fixed_contract_fixture import bind_main_contract
    from tests.unit.test_self_initiative_runtime import self_source

    from yuki.llm.fake import FakeLLMProvider

    source, _admissions, _binding = await self_source(database)
    repository = WorkRepository(database)
    database.subagents_enabled = True
    workers = SubagentRepository(repository)
    lease = await repository.acquire(source["conversation_id"], source["generation"])
    root = await repository.accept(
        lease, source_key=f"initiative:{source['initiative_run_id']}", source=source, goal="inspect"
    )
    identity = await workers.start(lease, root["id"], "actual-reader", {"goal": "check facts"})
    nested = await workers.start(lease, identity, "nested-fact", {"goal": "supply checked fact"})
    await repository.release(lease)
    nested_lease = await workers.acquire(nested)
    assert nested_lease is not None
    args = (
        {"slot": 1, "text": "Checked original nested result."}
        if tool_name == "update_short_state"
        else {"view": "recent"}
    )
    observed = []

    class NestedResultProvider(FakeLLMProvider):
        async def complete(self, request):
            response = await super().complete(request)
            if len(self.requests) == 1:
                await repository.accept_control(
                    nested_lease,
                    nested,
                    {"action": "complete", "call_key": "nested-done", "result": "checked fact"},
                )
                row = await repository.get(nested)
                await repository.transition(nested_lease, nested, row["revision"], "completed")
                await workers.finish(nested_lease)
                await repository.release(nested_lease)
            return response

    def respond(request):
        if len(provider.requests) == 1:
            return ChatResponse(
                "",
                0,
                tool_calls=(
                    ToolCall("continue-original-tool", ToolFunction(tool_name, json.dumps(args))),
                ),
            )
        observed.append(
            next(
                json.loads(message.content)
                for message in reversed(request.messages)
                if message.role == "tool"
            )
        )
        return "The original tool completed after receiving the nested fact."

    provider = NestedResultProvider(respond)
    harness = build_harness(
        database,
        make_settings(database.url, runtime_work_enabled=True, enabled_groups_csv="2001"),
        provider,
    )
    bind_main_contract(harness, tmp_path)
    chat = harness.processor._chat
    chat._tool_artifacts = ToolArtifactRepository(
        database, tmp_path / "artifacts", retention_seconds=60, max_artifact_bytes=100000
    )
    executor = make_child_executor(
        repository,
        chat=chat,
        config=chat._runtime_config,
        runner=chat.runtime.runner,
        load_tools=chat.runtime.runner.main_contract.definitions,
    )
    assert await executor.run(identity) is None
    assert observed[0]["ok"], observed[0]
    assert len(provider.requests) == 2
    assert (await repository.get(identity))["state"] == "completed"
    assert (await repository.get(nested))["state"] == "completed"
    if tool_name == "update_short_state":
        state = chat.runtime.runner.main_contract.state.snapshot()
        assert state[0]["text"] == "Checked original nested result."


async def test_derived_automation_rechecks_original_superuser_before_actual_effect(
    database, tmp_path, monkeypatch
):
    import time

    from tests.support.runtime_execution import make_work_resumer

    from yuki.automation.authority import PermissionLevel
    from yuki.automation.control_context import resolve_execution_identity
    from yuki.runtime.work_budget_schema import budgets
    from yuki.runtime.work_scheduler import WorkScheduler

    case = await setup_run(database, tmp_path, delivery="none", mode="silent", privileged=True)
    settings = case.settings
    assert await case.env.router.cas_takeover_person(case.env.person) == "taken"

    def respond(_request):
        if len(case.provider.requests) == 1:
            return ChatResponse(
                "",
                0,
                tool_calls=(
                    ToolCall(
                        "accept",
                        ToolFunction(
                            "task_control", '{"action":"accept","goal":"Original delegated work"}'
                        ),
                    ),
                    ToolCall(
                        "derive",
                        ToolFunction(
                            "task_control", '{"action":"derive","goal":"original delegated child"}'
                        ),
                    ),
                ),
            )
        elif len(case.provider.requests) == 3:
            name, args = (
                "update_short_state",
                {"slot": 1, "text": "this original authority was revoked in flight"},
            )
        else:
            name, args = "task_control", {"action": "complete", "result": "checked"}
        return ChatResponse(
            "",
            0,
            tool_calls=(
                ToolCall(
                    f"original:{len(case.provider.requests)}", ToolFunction(name, json.dumps(args))
                ),
            ),
        )

    case.provider._responder = respond
    original_complete = case.provider.complete

    async def complete(request):
        response = await original_complete(request)
        if len(case.provider.requests) == 3:
            # Revoke the Person qualification while the request is in flight.
            # Its accounts and original execution scene remain unchanged.
            async with database.immediate_session() as writer:
                await writer.execute(
                    update(CanonicalPersonModel)
                    .where(CanonicalPersonModel.id == case.env.person)
                    .values(is_superuser=False)
                )
        return response

    monkeypatch.setattr(case.provider, "complete", complete)
    case.clock.advance(2)
    first = await case.dispatch()
    assert first.status is RunStatus.SUCCEEDED
    assert len(case.provider.requests) == 2
    assert "update_short_state" in {tool.name for tool in case.provider.requests[0].tools}
    repository = WorkRepository(database)
    async with database.read_session() as reader:
        rows = list((await reader.execute(select(work))).mappings())
    parent = next(dict(row) for row in rows if row["parent_work_id"] is None)
    child = next(dict(row) for row in rows if row["parent_work_id"] == parent["id"])
    resumed_runtime = []

    async def capture_main(**kwargs):
        resumed_runtime.append(kwargs["source_runtime"])
        async with database.read_session() as reader:
            account, permission = await resolve_execution_identity(reader, owner_id=case.env.person)
            assert account == "" and permission is PermissionLevel.SUPERUSER
        return await case.chat.generate_origin_turn(**kwargs)

    resumer = make_work_resumer(
        repository,
        ledger=case.chat._ledger,
        scopes=case.chat._conversation_scopes,
        turns=case.chat._turn_coordinator,
        router=case.env.router,
        config=case.chat._runtime_config,
        generate_self=case.chat.generate_self_initiative,
        generate_wakeup=case.chat.generate_main_agent_wakeup,
        bindings=case.chat.runtime.bindings,
        settings=settings,
        generate_origin=capture_main,
    )

    scheduler = WorkScheduler(repository, resumer.resume, chat_admission_enabled=True)
    scheduler._last_reclaim = time.monotonic()
    await scheduler.drain_once()
    assert resumed_runtime[0].actor_is_superuser
    assert not any(action.startswith("send_") for action, _ in case.env.bot.calls)
    assert not any(
        item["text"] == "this original authority was revoked in flight"
        for item in case.chat.runtime.runner.main_contract.state.snapshot()
    )
    final = await repository.get(child["id"])
    assert final["state"] == "failed" and final["model_requests"] == 1
    assert len(case.provider.requests) == 3
    assert final["source_json"] == child["source_json"]
    original_parent = await repository.get(parent["id"])
    assert original_parent["state"] == "completed" and original_parent["model_requests"] == 2
    async with database.read_session() as reader:
        account, permission = await resolve_execution_identity(reader, owner_id=case.env.person)
        assert account == "" and permission is PermissionLevel.USER
        assert (
            await reader.scalar(select(budgets.c.models).where(budgets.c.root_id == parent["id"]))
            == 3
        )
