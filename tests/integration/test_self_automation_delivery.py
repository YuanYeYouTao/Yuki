"""SELF schedules retain their own actor and use the ordinary Main path."""

import json

import pytest
from sqlalchemy import func, select
from tests.support.automation_unified_delivery_helpers import sent, setup_run
from tests.support.runtime_execution import make_work_resumer

from qq_ai_bot.automation.models import RunStatus
from qq_ai_bot.conversation.autonomy_repository import AutonomyRepository
from qq_ai_bot.domain.messages import ChatResponse, ToolCall, ToolFunction
from qq_ai_bot.domain.tool_actor import ToolActor
from qq_ai_bot.memory.authorized_scope import AuthorizedMemoryScope
from qq_ai_bot.memory.models import MemoryScopeType
from qq_ai_bot.memory.self_origin import read_self_seed_candidates
from qq_ai_bot.persistence.models import (
    ChatEventModel,
    MemoryMutationReceiptModel,
    MemoryToolReceiptModel,
)
from qq_ai_bot.runtime.origin import TurnOrigin
from qq_ai_bot.runtime.work_schema_v1 import work
from qq_ai_bot.tool_results.recorder import ToolInvocationRepository


async def self_actor(database, env):
    admissions = AutonomyRepository(database)
    binding = await admissions.ensure_binding(env.context.conversation_id, 1)
    binding = await admissions.transition(binding, master_enabled=True, external_enabled=True)
    accepted = await admissions.accept_host_proposal(
        proposal_id="self-daily-agent",
        binding=binding,
        owner=binding.effective_owner,
        space_id=env.space,
        presence_id=env.presence,
        sources=(),
        support_refs=(),
        trigger_kind="intrinsic",
    )
    assert accepted.run is not None
    return ToolActor(
        user_id="",
        bot_user_id="80001",
        group_id="20001",
        origin=TurnOrigin.SELF_INITIATIVE,
        instruction="每天整理自己的工作",
        execution_id="self-work",
        conversation_id=env.context.conversation_id,
        presence_id=env.presence,
        principal_kind="self",
        initiative_run_id=accepted.run.run_id,
    )


@pytest.mark.parametrize("delivery", ["none", "current_group"])
async def test_self_schedule_uses_real_actor_without_preaccepted_work(database, tmp_path, delivery):
    case = await setup_run(
        database,
        tmp_path,
        principal="self",
        delivery=delivery,
        mode="silent" if delivery == "none" else "send",
    )
    sources = []
    generate = case.worker._generate

    async def capture(**kwargs):
        actor = kwargs["source_runtime"].require_actor()
        assert actor.principal_kind == "self" and actor.user_id == "" and actor.person_id is None
        assert kwargs["turn_snapshot"].trigger_event_id is None
        assert kwargs["turn_snapshot"].automation_run_id is not None
        sources.append(actor)
        return await generate(**kwargs)

    case.worker._generate = capture
    case.clock.advance(2)
    result = await case.dispatch()
    assert result.status is RunStatus.SUCCEEDED
    assert len(sources) == 1
    assert len(sent(case.env)) == (delivery == "current_group")
    assert case.provider.requests
    assert not await case.works.active(case.env.context.conversation_id, 1)
    before = len(case.provider.requests)
    assert (await case.dispatch()).id == result.id
    assert len(case.provider.requests) == before


@pytest.mark.parametrize("accept_work", [False, True])
async def test_self_schedule_memory_uses_original_execution_receipt_and_remains_readable(
    database, tmp_path, accept_work
):
    from tests.unit.test_self_initiative_memory import _service

    case = await setup_run(
        database,
        tmp_path,
        principal="self",
        delivery="none",
        mode="silent",
        self_memory_enabled=True,
    )
    mutations, facts, _, _ = _service(database, self_memory_enabled=True)
    case.chat._tools._memory_mutations = mutations
    case.chat._tool_invocations = ToolInvocationRepository(database)
    calls = []
    if accept_work:
        calls.append(("task_control", {"action": "accept", "goal": "记录真实工具经历"}))
    calls.extend(
        [
            ("get_group_members", {"limit": 10}),
            (
                "memory_change",
                {
                    "operation": "create",
                    "target": {"subject_ref": "self", "scope_type": "self"},
                    "new_content": "我实际查询了当前群成员列表，确认了 member 的成员资料。",
                    "memory_key": "self_episode:scheduled",
                    "category": "self_episode",
                    "kind": "episode",
                    "evidence_quote": "member",
                },
            ),
            ("get_memory_fact", None),
        ]
    )
    if accept_work:
        calls.append(("task_control", {"action": "complete", "result": "已保存真实记忆"}))
    responses = {}

    def respond(request):
        index = len(case.provider.requests) - 1
        if index:
            result = json.loads(request.messages[-1].content)
            previous = calls[index - 1][0]
            responses[previous] = result
            assert result["ok"], result
        if index >= len(calls):
            return "NO_REPLY"
        name, arguments = calls[index]
        if name == "get_memory_fact":
            arguments = {"fact_id": responses["memory_change"]["data"]["new_fact_id"]}
        return ChatResponse(
            "",
            0,
            tool_calls=(
                ToolCall(f"scheduled-memory-{index}", ToolFunction(name, json.dumps(arguments))),
            ),
        )

    case.provider._responder = respond
    case.clock.advance(2)
    result = await case.dispatch()
    assert result.status is RunStatus.SUCCEEDED, responses
    assert responses["memory_change"]["data"]["ok"]
    assert (
        responses["get_memory_fact"]["data"]["memory"]["content"]
        == calls[1 + int(accept_work)][1]["new_content"]
    )
    async with database.sessions() as session:
        mutation = await session.scalar(select(MemoryMutationReceiptModel))
        receipt = await session.scalar(
            select(MemoryToolReceiptModel).where(
                MemoryToolReceiptModel.tool_name == "get_group_members"
            )
        )
        actual_work = (await session.execute(select(work))).mappings().one_or_none()
        assert mutation.trigger_source_type == "execution"
        assert mutation.trigger_event_id is None and mutation.initiative_run_id is None
        assert receipt.trigger_event_id is None and receipt.initiative_run_id is None
        assert receipt.execution_id == mutation.decision_actor_id
        assert receipt.tool_call_id and receipt.source_call_key
        assert receipt.execution_id == f"automation:{result.id}:{case.row.script_hash}"
        if accept_work:
            source = json.loads(actual_work["source_json"])
            assert source["automation_run_id"] == result.id
            assert source["parent_execution_id"] == receipt.execution_id
            assert actual_work["state"] == "completed"
        else:
            assert actual_work is None
        assert await session.scalar(select(func.count(ChatEventModel.id))) == 1
        original = {
            column.name: getattr(mutation, column.name)
            for column in MemoryMutationReceiptModel.__table__.columns
        }
    fact = await facts.get_fact(mutation.new_fact_id)
    evidence = await facts.list_evidence(fact.id)
    assert fact.scope_type is MemoryScopeType.SELF and fact.visibility_group_id == "20001"
    assert len(evidence) == 1 and evidence[0].tool_receipt_id == receipt.id
    assert evidence[0].event_id is None
    assert [
        item.id
        for item in await read_self_seed_candidates(
            database, canonical_conversation_id=case.env.context.conversation_id
        )
    ] == [fact.id]
    requests = len(case.provider.requests)
    assert await case.dispatch() == result
    assert len(case.provider.requests) == requests and not sent(case.env)
    async with database.sessions() as session:
        after = await session.get(MemoryMutationReceiptModel, mutation.id)
        assert {
            column.name: getattr(after, column.name)
            for column in MemoryMutationReceiptModel.__table__.columns
        } == original


@pytest.mark.parametrize("work_enabled", [False, True])
async def test_self_schedule_sdk_read_records_its_actual_call_and_original_source(
    database, tmp_path, monkeypatch, caplog, work_enabled
):
    from qq_ai_bot.memory.self_origin import resolve_self_origin
    from qq_ai_bot.plugin_host import main_turn
    from qq_ai_bot.plugin_host.facades import HostPluginContext, PluginFacadeServices
    from qq_ai_bot.runtime.work_schema_v1 import effects
    from qq_ai_bot.sandbox.environment_tools import SANDBOX_TOOLS
    from qq_ai_bot.workspace.service import WorkspaceService
    from qq_ai_bot.workspace.tools import WORKSPACE_TOOLS
    from yuki_plugin_sdk.permissions import PluginPermission

    case = await setup_run(
        database,
        tmp_path,
        principal="self",
        delivery="none",
        mode="silent",
        work_enabled=work_enabled,
    )
    recorder = ToolInvocationRepository(database)
    case.chat._tool_invocations = recorder
    case.chat._tools.workspace_service = WorkspaceService(case.env.store)
    host = HostPluginContext(
        plugin_id="test.scheduled-self-read",
        approved_permissions=(PluginPermission.AGENT_RUN,),
        services=PluginFacadeServices(
            approval_revision="same-approved-read",
            ledger=case.harness.ledger,
            agent_runner=case.chat.runtime.runner,
            runtime_config=case.chat._runtime_config,
            agent_capabilities=SANDBOX_TOOLS | WORKSPACE_TOOLS,
        ),
    )
    runtimes, sources = [], []
    original_backend = main_turn.MainAgentBackend

    def backend(service, runtime, **kwargs):
        runtimes.append(runtime)
        return original_backend(service, runtime, **kwargs)

    monkeypatch.setattr(main_turn, "MainAgentBackend", backend)

    original_generate = case.worker._generate

    async def scheduled_source(**kwargs):
        sources.append(kwargs["source_runtime"])
        return await original_generate(**kwargs)

    case.worker._generate = scheduled_source
    case.provider._responder = lambda _: "Schedule source prepared"
    case.clock.advance(2)
    result = await case.dispatch()
    assert result.status is RunStatus.SUCCEEDED
    before = len(case.provider.requests)

    def respond(request):
        if len(case.provider.requests) == before + 1:
            return ChatResponse(
                "",
                0,
                tool_calls=(
                    ToolCall("sdk-approved-read", ToolFunction("workspace_list", "{}")),
                    ToolCall("sdk-not-granted", ToolFunction("memory_change", "{}")),
                ),
            )
        replies = {
            message.tool_call_id: json.loads(message.content)
            for message in request.messages
            if message.role == "tool"
        }
        assert replies["sdk-approved-read"]["ok"], replies
        assert replies["sdk-not-granted"]["error"] == "tool_not_declared", replies
        if work_enabled:
            return ChatResponse(
                "",
                0,
                tool_calls=(
                    ToolCall(
                        "sdk-finish",
                        ToolFunction(
                            "task_control", '{"action":"complete","result":"SDK read complete"}'
                        ),
                    ),
                ),
            )
        return "SDK read complete"

    case.provider._responder = respond

    try:
        with host.invocation_scope(host.plugin_id, sources[0], web_was_used=False):
            sdk_result = await host.agent.run(
                "Read published workspace artifacts through the approved SDK tool"
            )
        assert sdk_result.ok and sdk_result.data["text"] == "SDK read complete"
        root = f"automation:{result.id}:{case.row.script_hash}"
        assert len(runtimes) == 1
        actual = runtimes[0]
        assert actual.inbound is None and actual.require_actor().principal_kind == "self"
        assert actual.sandbox_source["parent_execution_id"] == root
        assert actual.execution_id.startswith(f"plugin:{host.plugin_id}:execution:{root}:")
        async with database.sessions() as session:
            receipts = list(
                await session.scalars(
                    select(MemoryToolReceiptModel).where(
                        MemoryToolReceiptModel.tool_name == "workspace_list"
                    )
                )
            )
            assert len(receipts) == 1
            receipt = receipts[0]
            assert receipt.success and receipt.execution_id == actual.execution_id
            assert receipt.trigger_event_id is None and receipt.initiative_run_id is None
            assert receipt.tool_call_id and receipt.source_call_key
            assert (
                receipt.canonical_person_id is None and receipt.canonical_space_id == case.env.space
            )
            resolved = await resolve_self_origin(
                session,
                execution_id=receipt.execution_id,
                require_live=False,
                require_group_projection=False,
            )
            assert resolved.space_id == case.env.space and resolved.presence_id == case.env.presence
            saved = (await session.execute(select(work))).mappings().one_or_none()
            if work_enabled:
                source = json.loads(saved["source_json"])
                assert source["execution_source"]["parent_execution_id"] == root
                assert source["parent_execution_id"] == actual.execution_id
                assert saved["state"] == "completed"
                resolved_work = await resolve_self_origin(
                    session,
                    execution_id=saved["id"],
                    require_live=False,
                    require_group_projection=False,
                )
                assert resolved_work == resolved
                assert list(await session.scalars(select(effects.c.state)))
            else:
                assert saved is None
            assert await session.scalar(select(func.count(MemoryMutationReceiptModel.id))) == 0
            assert await session.scalar(select(func.count(ChatEventModel.id))) == 1
        # Repair/reentry keeps the SDK call key without another tool/model request.
        original_calls = len(case.provider.requests)
        await recorder.record_invocation(
            conversation_key=actual.conversation_key,
            provider_id=receipt.provider_id,
            tool_name=receipt.tool_name,
            success=True,
            latency_seconds=0,
            result_size=receipt.result_characters,
            artifact_created=False,
            error_category=None,
            result_excerpt=receipt.result_excerpt,
            canonical_conversation_id=actual.effective_conversation_id,
            principal_kind="self",
            execution_id=receipt.execution_id,
            tool_call_id=receipt.tool_call_id,
        )
        async with database.sessions() as session:
            assert (
                await session.scalar(
                    select(func.count(MemoryToolReceiptModel.id)).where(
                        MemoryToolReceiptModel.tool_name == "workspace_list"
                    )
                )
                == 1
            )
        assert len(case.provider.requests) == original_calls and not sent(case.env)
        assert "coverage_incomplete" not in caplog.text
    finally:
        await host.close_host_resources()


async def test_self_scheduled_work_memory_keeps_original_version_after_task_update(
    database, tmp_path
):
    from tests.unit.test_self_initiative_memory import _service

    case = await setup_run(
        database,
        tmp_path,
        principal="self",
        delivery="none",
        mode="silent",
        self_memory_enabled=True,
        trigger={"type": "interval", "seconds": 60},
    )
    mutations, facts, _, _ = _service(database, self_memory_enabled=True)
    case.chat._tools._memory_mutations = mutations
    case.chat._tool_invocations = ToolInvocationRepository(database)

    def parent_response(request):
        calls = (
            (
                ToolCall(
                    "accept-original",
                    ToolFunction("task_control", '{"action":"accept","goal":"原任务"}'),
                ),
                ToolCall(
                    "derive-original",
                    ToolFunction(
                        "task_control", '{"action":"derive","goal":"沿真实工具写入自己的记忆"}'
                    ),
                ),
            )
            if len(case.provider.requests) == 1
            else (
                ToolCall(
                    "complete-original",
                    ToolFunction("task_control", '{"action":"complete","result":"已派发原任务"}'),
                ),
            )
        )
        return ChatResponse("", 0, tool_calls=calls)

    case.provider._responder = parent_response
    case.clock.advance(61)
    original_run = await case.dispatch()
    assert original_run.status is RunStatus.SUCCEEDED
    async with database.sessions() as session:
        rows = [dict(row) for row in (await session.execute(select(work))).mappings()]
    child = next(row for row in rows if row["parent_work_id"] is not None)
    source = json.loads(child["source_json"])
    assert source["automation_run_id"] == original_run.id
    updated = await case.service.update_task(
        case.row.id,
        {
            "name": "new definition",
            "goal": "新的任务定义",
            "trigger": {"type": "interval", "seconds": 60},
            "delivery": {"target": "none"},
        },
        actor=case.actor,
        conversation_key="group:80001:20001",
    )
    assert updated.script_hash != case.row.script_hash
    child_turns = 0
    responses = {}
    write = {
        "operation": "create",
        "target": {"subject_ref": "self", "scope_type": "self"},
        "new_content": "我在原定时任务的派生工作中查询了 member 的真实成员资料。",
        "memory_key": "self_episode:original-work",
        "category": "self_episode",
        "kind": "episode",
        "evidence_quote": "member",
    }
    calls = (
        ("get_group_members", {}),
        ("memory_change", write),
        ("get_memory_fact", None),
        ("task_control", {"action": "complete", "result": "原工作已写入记忆"}),
    )

    def child_response(request):
        nonlocal child_turns
        index = child_turns
        child_turns += 1
        if index:
            result = json.loads(request.messages[-1].content)
            responses[calls[index - 1][0]] = result
            assert result["ok"], result
        if index >= len(calls):
            return "NO_REPLY"
        name, arguments = calls[index]
        if name == "get_memory_fact":
            arguments = {"fact_id": responses["memory_change"]["data"]["new_fact_id"]}
        return ChatResponse(
            "",
            0,
            tool_calls=(
                ToolCall(f"original-work-{index}", ToolFunction(name, json.dumps(arguments))),
            ),
        )

    case.provider._responder = child_response
    source_runtimes = []

    async def capture(**kwargs):
        source_runtimes.append(kwargs["source_runtime"])
        return await case.chat.generate_origin_turn(**kwargs)

    resumer = make_work_resumer(
        case.works,
        ledger=case.chat._ledger,
        scopes=case.chat._conversation_scopes,
        turns=case.chat._turn_coordinator,
        router=case.env.router,
        config=case.chat._runtime_config,
        generate_self=case.chat.generate_self_initiative,
        generate_wakeup=case.chat.generate_main_agent_wakeup,
        bindings=case.chat.runtime.bindings,
        settings=case.settings,
        generate_origin=capture,
    )
    await resumer.resume(child)
    assert (await case.works.get(child["id"]))["state"] == "completed", responses
    assert len(source_runtimes) == 1
    actor = source_runtimes[0].require_actor()
    assert actor.execution_id == child["id"] and actor.automation_run_id == original_run.id
    assert actor.principal_kind == "self" and actor.initiative_run_id is None
    assert actor.person_id is None and actor.event_id is None and actor.user_id == ""
    assert responses["memory_change"]["data"]["ok"]
    assert responses["get_memory_fact"]["data"]["memory"]["content"] == write["new_content"]
    async with database.sessions() as session:
        receipt = await session.scalar(
            select(MemoryToolReceiptModel).where(
                MemoryToolReceiptModel.tool_name == "get_group_members"
            )
        )
        mutation = await session.scalar(select(MemoryMutationReceiptModel))
        assert receipt.execution_id == mutation.decision_actor_id == child["id"]
        assert receipt.initiative_run_id is None and receipt.trigger_event_id is None
    assert [
        item.id
        for item in await read_self_seed_candidates(
            database, canonical_conversation_id=case.env.context.conversation_id
        )
    ] == [mutation.new_fact_id]
    before = len(case.provider.requests)
    await resumer.resume(await case.works.get(child["id"]))
    assert len(case.provider.requests) == before and not sent(case.env)
    original_source = (await case.works.get(child["id"]))["source_json"]
    lease = await case.works.acquire(child["conversation_id"], child["generation"])
    for index in range(130):
        recent = await case.works.accept(
            lease, source_key=f"recent-terminal:{index}", source={}, goal="已完成的工作记录"
        )
        await case.works.transition(lease, recent["id"], recent["revision"], "completed")
    await case.works.release(lease)
    await case.works.reclaim_terminal()
    await case.works.reclaim_terminal()
    retained = await case.works.get(child["id"])
    assert json.loads(retained["checkpoint_json"])["archived"] is True
    assert (
        json.loads(retained["source_json"])["parent_execution_id"] == source["parent_execution_id"]
    )
    assert json.loads(original_source)["parent_execution_id"] == source["parent_execution_id"]
    visible = await facts.repository.get_active_authorized(
        AuthorizedMemoryScope(
            current_space_id=case.env.space,
            current_group_only=True,
            allowed_scopes=(MemoryScopeType.SELF,),
        ),
        (mutation.new_fact_id,),
    )
    assert [fact.id for fact in visible] == [mutation.new_fact_id]
    assert [
        fact.id
        for fact in await read_self_seed_candidates(
            database, canonical_conversation_id=case.env.context.conversation_id
        )
    ] == [mutation.new_fact_id]
    async with database.sessions() as session:
        current = await session.get(MemoryMutationReceiptModel, mutation.id)
        assert current.decision_actor_id == receipt.execution_id == child["id"]
    assert len(case.provider.requests) == before and not sent(case.env)
