"""Regressions for neutral answers and shared execution authority."""

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from tests.conftest import build_harness, make_settings
from tests.support.social_identity_cases import social_env

from qq_ai_bot.automation.models import TurnOrigin
from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.domain.messages import ChatMessage, ToolCall, ToolFunction
from qq_ai_bot.llm.fake import FakeLLMProvider
from qq_ai_bot.runtime.work_activation import activate_work
from qq_ai_bot.runtime.work_repository import WorkRepository
from qq_ai_bot.services.agent_runner import AgentRuntime
from qq_ai_bot.services.agent_tools import ToolRuntime
from qq_ai_bot.services.main_agent_backend import MainAgentBackend
from qq_ai_bot.services.main_agent_contract import MainAgentContract
from qq_ai_bot.workspace.short_state import ShortState
from qq_ai_bot.workspace.store import WorkspaceStore


@pytest.mark.asyncio
async def test_neutral_answer_is_delivered_without_registration_request(database, tmp_path):
    env = await social_env(database, tmp_path)
    provider = FakeLLMProvider(lambda request: "Yuki 是用 Python 写的。")
    chat = build_harness(database, make_settings(database.url), provider).processor._chat
    chat._agent_runner.main_contract = MainAgentContract(
        chat, SimpleNamespace(_registry=None), ShortState(WorkspaceStore(tmp_path / "state"))
    )
    config = await chat._runtime_config.snapshot()

    async def validate():
        pass

    async with activate_work(
        WorkRepository(database),
        env.context.conversation_id,
        1,
        "neutral-answer",
        {"origin": "user_message"},
        validate,
    ) as control:
        runtime = AgentRuntime(
            origin=TurnOrigin.USER_MESSAGE,
            actor_user_id="10001",
            actor_is_superuser=False,
            delegated_authority=None,
            conversation_key="neutral-answer",
            current_group_id=None,
            bot_user_id="7777",
            gateway=None,
            runtime_config=config,
            current_time=chat._time.current_default(),
            allowed_capabilities=frozenset(),
            max_tool_calls=32,
            max_model_requests=24,
            work_control=control,
            dynamic_context_prepared=True,
        )
        backend = MainAgentBackend(
            chat,
            ToolRuntime(
                inbound=None,
                gateway=None,
                allow_generic_onebot=False,
                runtime_config=config,
                scope_type=ScopeType.PRIVATE,
            ),
        )
        result = await chat._agent_runner.run(
            (ChatMessage(role="user", content="Yuki 是用 Python 写的吗？"),),
            runtime,
            backend,
        )
        assert result.text == "Yuki 是用 Python 写的。"
        assert result.model_requests == 1
        assert control.current is None
        assert control.corrections == 0
    assert len(provider.requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "origin",
    [TurnOrigin.SCHEDULED_AUTOMATION, TurnOrigin.PLUGIN_SESSION, TurnOrigin.PLUGIN_BACKGROUND],
)
async def test_host_granted_environment_reaches_shared_executor(database, tmp_path, origin):
    chat = build_harness(database, make_settings(database.url)).processor._chat
    chat._agent_runner.main_contract = MainAgentContract(
        chat, SimpleNamespace(_registry=None), ShortState(WorkspaceStore(tmp_path / "state"))
    )
    calls = []

    async def execute(name, arguments, **kwargs):
        calls.append((name, arguments))
        return {"path": arguments["path"], "version": "written"}

    chat._tools.workspace_service = SimpleNamespace(execute=execute)
    config = await chat._runtime_config.snapshot()
    runtime = AgentRuntime(
        origin=origin,
        actor_user_id="10001",
        actor_is_superuser=False,
        delegated_authority=None,
        conversation_key="environment-test",
        current_group_id=None,
        bot_user_id="7777",
        gateway=None,
        runtime_config=config,
        current_time=chat._time.current_default(),
        allowed_capabilities=frozenset(),
        max_tool_calls=32,
        max_model_requests=24,
    )
    tool_runtime = ToolRuntime(
        inbound=None,
        gateway=None,
        allow_generic_onebot=False,
        origin=origin,
        scope_type=ScopeType.PRIVATE,
        bot_user_id="7777",
        actor_user_id="10001",
        execution_id="environment-test",
        runtime_config=config,
        allow_work_environment=True,
    )
    call = ToolCall(
        "write",
        ToolFunction("workspace_write", json.dumps({"path": "测试.txt", "text": "hello"})),
    )
    for allowed in (False, True):
        backend = MainAgentBackend(
            chat,
            replace(tool_runtime, allow_work_environment=allowed),
            allowed_tools=frozenset({"workspace_write"}),
        )
        await backend.prepare(runtime)
        backend.begin_batch((call,), runtime)
        result = json.loads(
            await backend.execute(call.function.name, call.function.arguments, runtime)
        )
        assert result["ok"] is allowed, result
    assert calls == [("workspace_write", {"path": "测试.txt", "text": "hello"})]


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", [TurnOrigin.SCHEDULED_AUTOMATION, TurnOrigin.PLUGIN_SESSION])
@pytest.mark.parametrize("segment_limit", [12, 24])
async def test_owned_main_turn_resumes_original_journal_and_budget(
    database, tmp_path, origin, segment_limit
):
    from qq_ai_bot.domain.messages import ChatResponse

    env = await social_env(database, tmp_path)
    writes = []
    provider = FakeLLMProvider()

    def respond(request):
        number = len(provider.requests)
        if number <= 26:
            return ChatResponse(
                content="",
                latency_seconds=0,
                tool_calls=(
                    ToolCall(
                        f"write-{number}",
                        ToolFunction(
                            "workspace_write",
                            json.dumps({"path": f"file-{number}.txt", "text": f"part {number}"}),
                        ),
                    ),
                ),
            )
        return "三个文件已经写好。"

    provider._responder = respond
    chat = build_harness(
        database, make_settings(database.url, runtime_work_enabled=True), provider
    ).processor._chat
    chat._agent_runner.main_contract = MainAgentContract(
        chat, SimpleNamespace(_registry=None), ShortState(WorkspaceStore(tmp_path / "state"))
    )

    async def execute(name, arguments, **kwargs):
        writes.append(arguments["path"])
        return {"path": arguments["path"], "version": "written"}

    chat._tools.workspace_service = SimpleNamespace(execute=execute)
    config = await chat._runtime_config.snapshot()
    runtime = AgentRuntime(
        origin=origin,
        actor_user_id="10001",
        actor_is_superuser=False,
        delegated_authority=None,
        conversation_key="owned-work",
        current_group_id="20001",
        bot_user_id="80001",
        gateway=None,
        runtime_config=config,
        current_time=chat._time.current_default(),
        allowed_capabilities=frozenset({"workspace_write"}),
        max_tool_calls=32,
        max_model_requests=segment_limit,
        canonical_conversation_id=env.context.conversation_id,
        execution_id="same-owned-execution",
        invocation_goal="写三个文件",
        invocation_source={"owner": "test"},
        dynamic_context_prepared=True,
    )
    tool_runtime = ToolRuntime(
        inbound=None,
        gateway=None,
        allow_generic_onebot=False,
        origin=origin,
        scope_type=ScopeType.GROUP,
        actor_user_id="10001",
        bot_user_id="80001",
        runtime_config=config,
        allow_work_environment=True,
        conversation_id=env.context.conversation_id,
        conversation_key="owned-work",
        current_group_id="20001",
        execution_id="same-owned-execution",
    )

    async def run(text):
        return await chat._main_turns.run(
            (ChatMessage(role="user", content=text),),
            runtime,
            MainAgentBackend(chat, tool_runtime, allowed_tools=frozenset({"workspace_write"})),
        )

    first = await run("写三个文件")
    assert first.work_state == "queued", first
    assert first.work_id
    second = await run("this new assembly must not replace the saved request prefix")
    if segment_limit == 12:
        assert second.work_state == "queued", second
        second = await run("third segment also keeps the same chain")
    assert second.work_state == "completed", second
    assert second.work_id == first.work_id
    assert second.text == "三个文件已经写好。"
    assert writes == [f"file-{index}.txt" for index in range(1, 27)], [
        message.content
        for request in provider.requests
        for message in request.messages
        if message.role == "tool"
    ]
    assert len(provider.requests) == 27
    for previous, following in zip(provider.requests, provider.requests[1:], strict=False):
        assert following.messages[: len(previous.messages)] == previous.messages
        assert following.tools == previous.tools
    record = await WorkRepository(database).get(first.work_id)
    assert record["model_requests"] == 27
    assert record["tool_calls"] == 26
    repeated = await run("ignored repeat")
    assert repeated.work_id == first.work_id and repeated.text == second.text
    assert len(provider.requests) == 27


@pytest.mark.asyncio
@pytest.mark.parametrize("segment_resume", [False, True, "preparation", "question"])
async def test_plugin_callback_pending_is_queryable_after_callback_returns(
    database, tmp_path, monkeypatch, segment_resume
):
    import asyncio

    from sqlalchemy import select

    from qq_ai_bot.domain.messages import InboundMessage, SenderIdentity
    from qq_ai_bot.persistence.models import ChatEventModel
    from qq_ai_bot.plugin_host import main_turn
    from qq_ai_bot.plugin_host.facades import (
        HostPluginContext,
        PluginFacadeServices,
        PluginInvocation,
    )
    from yuki_plugin_sdk.permissions import PluginPermission

    env = await social_env(database, tmp_path)
    released = asyncio.Event()

    class SlowProvider(FakeLLMProvider):
        async def complete(self, request):
            await released.wait()
            return await super().complete(request)

    provider = SlowProvider(lambda request: "已完成计算")
    if segment_resume:
        from qq_ai_bot.domain.messages import ChatResponse

        released.set()

        def respond(request):
            if len(provider.requests) == 1 and segment_resume == "question":
                return ChatResponse(
                    content="",
                    latency_seconds=0,
                    tool_calls=(
                        ToolCall(
                            "ask",
                            ToolFunction(
                                "task_control",
                                json.dumps(
                                    {
                                        "action": "need_input",
                                        "reason": "需要哪个颜色？",
                                    }
                                ),
                            ),
                        ),
                    ),
                )
            if len(provider.requests) == 1:
                return ChatResponse(
                    content="",
                    latency_seconds=0,
                    tool_calls=(
                        ToolCall(
                            "remember",
                            ToolFunction(
                                "update_short_state",
                                json.dumps(
                                    {
                                        "slot": 1,
                                        "text": "计算中",
                                        "expected_revision": 0,
                                    }
                                ),
                            ),
                        ),
                    ),
                )
            return "已完成计算"

        provider._responder = respond
    chat = build_harness(
        database, make_settings(database.url, runtime_work_enabled=True), provider
    ).processor._chat
    chat._agent_runner.main_contract = MainAgentContract(
        chat, SimpleNamespace(_registry=None), ShortState(WorkspaceStore(tmp_path / "state"))
    )
    host = HostPluginContext(
        plugin_id="test.pending",
        approved_permissions=[PluginPermission.AGENT_RUN],
        services=PluginFacadeServices(
            ledger=chat._ledger,
            agent_runner=chat._agent_runner,
            runtime_config=chat._runtime_config,
        ),
    )
    async with database.sessions() as session:
        event = await session.scalar(
            select(ChatEventModel).where(ChatEventModel.direction == "inbound")
        )
    inbound = InboundMessage(
        message_id=event.platform_message_id,
        source_event_id=event.id,
        event_type="message",
        scope_type=ScopeType.GROUP,
        sender=SenderIdentity("10001"),
        text="计算",
        bot_user_id="80001",
        group_id="20001",
        person_id=env.person,
        space_id=env.space,
        presence_id=env.presence,
        conversation_id=env.context.conversation_id,
        legacy_conversation_key="group:80001:20001",
    )
    invocation = PluginInvocation(
        plugin_id=host.plugin_id,
        origin=TurnOrigin.USER_MESSAGE,
        actor_user_id="10001",
        bot_user_id="80001",
        inbound=inbound,
    )
    monkeypatch.setattr(main_turn, "CALLBACK_WAIT_SECONDS", 0.5)
    try:
        if segment_resume == "preparation":
            from yuki_plugin_sdk.errors import PluginPermissionError

            async def stuck_preparation(**kwargs):
                await asyncio.Future()

            monkeypatch.setattr(chat._context_assembler, "assemble_plugin", stuck_preparation)
            with (
                host.bind(invocation),
                pytest.raises(PluginPermissionError, match="no work accepted"),
            ):
                await asyncio.wait_for(host.agent.run("计算"), 3)
            assert not main_turn._RUNNING
            assert not provider.requests
            return
        with host.bind(invocation):
            pending = await asyncio.wait_for(
                host.agent.run("计算", max_model_requests=1 if segment_resume else None), 3
            )
        assert pending.data["pending"] is True, pending
        work_id = pending.data["work_id"]
        if segment_resume:
            # Pending may be returned while the first segment is still committing
            # on a slower runner. Wait for that activation, not an assumed timer.
            tasks = tuple(main_turn._RUNNING.values())
            if tasks:
                await asyncio.wait_for(asyncio.gather(*tasks), 10)
            row = await WorkRepository(database).get(work_id)
            if segment_resume == "question":
                assert row["state"] == "waiting_user"
                assert (await host.agent.result(work_id)).data["reason"] == "需要哪个颜色？"
                answer = await host.agent.resume(work_id, "蓝色", request_id="color-answer")
                assert answer.ok and answer.data["state"] == "queued", answer
                repeated = await host.agent.resume(work_id, "蓝色", request_id="color-answer")
                assert repeated.ok and repeated.data["work_id"] == work_id
                conflict = await host.agent.resume(work_id, "红色", request_id="color-answer")
                assert not conflict.ok and conflict.error_code == "work_input_conflict"
                row = await WorkRepository(database).get(work_id)
            assert row["state"] == "queued", row
            await main_turn.resume_plugin_work(
                SimpleNamespace(_plugin_contexts={host.plugin_id: host}, ledger=chat._ledger),
                row,
                json.loads(row["source_json"]),
            )
            assert (
                provider.requests[1].messages[: len(provider.requests[0].messages)]
                == provider.requests[0].messages
            )
            assert provider.requests[1].tools == provider.requests[0].tools
        else:
            tasks = tuple(main_turn._RUNNING.values())
            assert tasks and not tasks[0].done()
            released.set()
            await asyncio.wait_for(asyncio.gather(*tasks), 3)
        # The original invocation ContextVar has been cleared; ownership comes from storage.
        final = await host.agent.result(work_id)
        assert final.data["state"] == "completed", final
        assert final.data["text"] == "已完成计算"
        assert len(provider.requests) == (2 if segment_resume else 1)
        again = await host.agent.result(work_id)
        assert again == final
        from qq_ai_bot.runtime.work_repository import WorkConflict
        from yuki_plugin_sdk.errors import PluginPermissionError

        if not segment_resume:
            import time
            from uuid import uuid4

            from sqlalchemy import insert, update

            from qq_ai_bot.runtime.work_schema_v1 import work

            original = await WorkRepository(database).get(work_id)
            async with database.immediate_session() as session:
                await session.execute(
                    insert(work),
                    [
                        {
                            **original,
                            "id": str(uuid4()),
                            "source_key": f"archive-pressure-{index}",
                            "source_json": "{}",
                            "checkpoint_json": "{}",
                            "updated": time.time() + index,
                        }
                        for index in range(130)
                    ],
                )
            await WorkRepository(database).reclaim_terminal()
            assert (await host.agent.result(work_id)).data["text"] == "已完成计算"
            async with database.immediate_session() as session:
                await session.execute(
                    update(work).where(work.c.id == work_id).values(updated=time.time() - 8 * 86400)
                )
            await WorkRepository(database).reclaim_terminal()
            assert (await host.agent.result(work_id)).error_code == "work_archived"
            with host.bind(invocation):
                archived = await host.agent.run("计算")
            assert archived.data["state"] == "archived" and archived.data["work_id"] == work_id
            assert len(provider.requests) == 1

        host._services = replace(host._services, approval_revision="changed-approval")
        with pytest.raises(PluginPermissionError):
            await host.agent.result(work_id)
        with host.bind(invocation), pytest.raises(WorkConflict, match="authority_changed"):
            await host.agent.run("计算", max_model_requests=1 if segment_resume else None)
        assert len(provider.requests) == (2 if segment_resume else 1)
    finally:
        released.set()
        await main_turn.close_plugin_main_tasks(host.plugin_id)


@pytest.mark.asyncio
async def test_scoped_background_reads_do_not_use_send_route_or_expand_scope(database, tmp_path):
    from qq_ai_bot.domain.conversations import ConversationScope

    env = await social_env(database, tmp_path)
    chat = build_harness(database, make_settings(database.url)).processor._chat
    chat._tools.social_service = env.service
    chat._agent_runner.main_contract = MainAgentContract(
        chat, SimpleNamespace(_registry=None), ShortState(WorkspaceStore(tmp_path / "state"))
    )
    config = await chat._runtime_config.snapshot()
    runtime = AgentRuntime(
        origin=TurnOrigin.SCHEDULED_AUTOMATION,
        actor_user_id="10001",
        actor_is_superuser=False,
        delegated_authority=None,
        conversation_key="read",
        current_group_id="20001",
        bot_user_id="80001",
        gateway=None,
        runtime_config=config,
        current_time=chat._time.current_default(),
        allowed_capabilities=frozenset(),
        max_tool_calls=32,
        max_model_requests=24,
    )
    backend = MainAgentBackend(
        chat,
        ToolRuntime(
            inbound=None,
            gateway=None,
            allow_generic_onebot=False,
            runtime_config=config,
            origin=runtime.origin,
            actor_user_id="10001",
            current_group_id="20001",
            bot_user_id="80001",
            scope_type=ScopeType.GROUP,
            conversation_id=env.context.conversation_id,
            execution_id="scoped-read",
            read_scope=ConversationScope.group("80001", "20001"),
            read_target_id=env.space,
            history_limit=20,
        ),
        allowed_tools=frozenset(
            {"get_recent_chat_history", "search_chat_history", "read_conversation_history"}
        ),
    )
    await backend.prepare(runtime)
    for name, args, allowed in (
        ("get_recent_chat_history", {}, True),
        ("search_chat_history", {"keyword": "hello", "group_id": "99999"}, False),
        ("read_conversation_history", {"kind": "space"}, True),
        ("read_conversation_history", {"kind": "person", "target_id": env.person}, False),
    ):
        call = ToolCall(name, ToolFunction(name, json.dumps(args)))
        backend.begin_batch((call,), runtime)
        result = json.loads(await backend.execute(name, call.function.arguments, runtime))
        assert result["ok"] is allowed, result
        if name == "get_recent_chat_history":
            assert result["data"]["source"] == "ledger"
            assert result["data"]["events"][0]["content"] == "hello"
    assert any(action == "get_group_msg_history" for action, _ in env.bot.calls)


@pytest.mark.asyncio
async def test_creation_replay_and_run_budget_are_atomic(database, tmp_path):
    import asyncio
    from datetime import UTC, datetime

    from tests.unit.test_automation_runtime import FakeClock, _inbound, _script

    from qq_ai_bot.automation.registry import build_capability_registry
    from qq_ai_bot.automation.repository import AutomationRepository
    from qq_ai_bot.automation.service import AutomationService
    from qq_ai_bot.capabilities.invocation import ToolInvocationContext, current_invocation
    from qq_ai_bot.runtime.work_budget import WorkBudgetExceeded
    from qq_ai_bot.time.service import TimeContextService

    repository = AutomationRepository(database)
    clock = FakeClock(datetime(2026, 9, 14, tzinfo=UTC))
    service = AutomationService(
        settings=make_settings(database.url, automation_enabled=True),
        repository=repository,
        registry=build_capability_registry(),
        time_service=TimeContextService(database, clock=clock),
    )

    async def create(call_id, script):
        token = current_invocation.set(
            ToolInvocationContext(
                runtime=None,
                call_id=call_id,
                execution_id="one-owned-invocation",
            )
        )
        try:
            return await service.create(
                script, inbound=_inbound(), conversation_key="private:10001"
            )
        finally:
            current_invocation.reset(token)

    first, repeated = await asyncio.gather(
        create("same-call", _script()), create("same-call", _script())
    )
    assert first.id == repeated.id
    another = await create("another-call", _script())
    assert another.id != first.id
    changed = _script().model_copy(update={"name": "changed task"})
    with pytest.raises(ValueError):
        await create("same-call", changed)

    task = {
        "name": "reminder",
        "goal": "喝水",
        "strategy": "static",
        "trigger": {"type": "once", "local_datetime": "2026-09-15T12:00:00"},
        "delivery": {"target": "self_private"},
    }
    created, _ = await service.create_task(
        task, inbound=_inbound(), conversation_key="private:10001"
    )
    same = await service.find_equivalent_task({**task, "name": "另一个名字"}, inbound=_inbound())
    assert [row.id for row in same] == [created.id]
    assert not await service.find_equivalent_task({**task, "goal": "吃饭"}, inbound=_inbound())

    run = await repository.create_run(
        first.id, scheduled_for=first.next_run_at, actual_started_at=clock.now()
    )
    env = await social_env(database, tmp_path)
    work_repository = WorkRepository(database)

    async def validate():
        pass

    async def accepted(key):
        return activate_work(
            work_repository,
            env.context.conversation_id,
            1,
            key,
            {
                "owner": "automation",
                "automation_run_id": run.id,
                "origin": "scheduled_automation",
                "actor_user_id": "10001",
            },
            validate,
        )

    async with await accepted("step-one") as control:
        await control.execute(
            "task_control",
            {"action": "accept", "goal": "part one", "output_kind": "answer"},
            "admit",
        )
        await work_repository.checkpoint(control.lease, control.current["id"], None, models=119)
        control.ending = "waiting_user"
    async with await accepted("step-two") as control:
        await control.execute(
            "task_control",
            {"action": "accept", "goal": "part two", "output_kind": "answer"},
            "admit",
        )
        with pytest.raises(WorkBudgetExceeded):
            await work_repository.checkpoint(control.lease, control.current["id"], None, models=2)
        row = await work_repository.get(control.current["id"])
        assert row["model_requests"] == 0
        await work_repository.checkpoint(control.lease, control.current["id"], None, models=1)
        control.ending = "waiting_user"

    # A lease stolen after a long external wait cannot overwrite the new owner's cursor/result.
    from datetime import timedelta

    from qq_ai_bot.automation.executor import AutomationExecutionError
    from qq_ai_bot.automation.models import RunStatus
    from qq_ai_bot.automation.work_cursor import save

    due = first.next_run_at
    await repository.claim_due(worker_id="old", now=due, lease_seconds=1, limit=20)
    await repository.claim_due(
        worker_id="new", now=due + timedelta(seconds=2), lease_seconds=30, limit=20
    )
    with pytest.raises(AutomationExecutionError, match="automation_lease_lost"):
        await save(database, run.id, first.script_hash, "ready", {}, expected_owner="old")
    await save(database, run.id, first.script_hash, "ready", {}, expected_owner="new")
    values = dict(
        status=RunStatus.SUCCEEDED,
        steps_completed=1,
        llm_calls=0,
        tool_calls=1,
        messages_sent=0,
        error_category=None,
        summary={},
        finished_at=due,
    )
    assert not await repository.finish_run(run.id, worker_id="old", **values)
    assert await repository.finish_run(run.id, worker_id="new", **values)


@pytest.mark.asyncio
async def test_plugin_handler_failure_after_write_is_not_replayed(tmp_path):
    from unittest.mock import AsyncMock

    from pydantic import BaseModel

    from qq_ai_bot.plugin_host.capability_adapter import PluginCapabilityAdapter
    from qq_ai_bot.plugin_host.extension_registry import ExtensionKind
    from yuki_plugin_sdk.models import RetryPolicy, RiskClass
    from yuki_plugin_sdk.registrar import ToolMetadata, ToolRegistration

    class Arguments(BaseModel):
        pass

    for risk, failure, expected in (
        (RiskClass.MUTATE, OSError, 1),
        (RiskClass.MUTATE, RuntimeError, 1),
        (RiskClass.READ, OSError, 2),
    ):
        writes = []

        async def handler(arguments, writes=writes, risk=risk, failure=failure):
            writes.append(True)
            if risk is RiskClass.MUTATE:
                (tmp_path / "committed.txt").write_text("written", encoding="utf-8")
            raise failure("after handler work")

        registration = ToolRegistration(
            ToolMetadata(
                name="operation",
                description="test operation",
                risk=risk,
                retry_policy=RetryPolicy.TRANSIENT_ONCE,
            ),
            Arguments,
            Arguments,
            handler,
        )
        item = SimpleNamespace(
            kind=ExtensionKind.TOOL, plugin_id="test.effect", registration=registration
        )
        adapter = PluginCapabilityAdapter(
            registry=SimpleNamespace(resolve_model_name=lambda name, item=item: item),
            installations=SimpleNamespace(
                get=AsyncMock(return_value=SimpleNamespace(enabled=True))
            ),
        )
        runtime = ToolRuntime(inbound=None, gateway=None, allow_generic_onebot=False)
        result = json.loads(await adapter.execute("operation", "{}", runtime, web_was_used=False))
        assert len(writes) == expected
        assert not result["ok"]
        if risk is RiskClass.MUTATE:
            assert result["uncertain"] is True and result["mutation_committed"] is None
            assert (tmp_path / "committed.txt").read_text(encoding="utf-8") == "written"
