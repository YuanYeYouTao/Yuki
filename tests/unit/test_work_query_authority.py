"""Work directory reads share real Automation authority and the Runner gate."""

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from tests.conftest import build_harness, make_settings

from qq_ai_bot.automation.models import TurnOrigin
from qq_ai_bot.automation.tools import AutomationToolService
from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.domain.messages import InboundMessage, SenderIdentity, ToolCall, ToolFunction
from qq_ai_bot.domain.tool_actor import ToolActor
from qq_ai_bot.runtime.contracts import MemoryCapabilityView
from qq_ai_bot.services.agent_runner import AgentRuntime
from qq_ai_bot.services.agent_tools import ToolRuntime
from qq_ai_bot.services.main_agent_backend import MainAgentBackend


@pytest.fixture
async def query_environment(database):
    chat = build_harness(database, make_settings(database.url)).processor._chat
    # Use the production definitions, registry and capability policy. No domain
    # operation is dispatched while checking read authority.
    chat.set_automation_tools(AutomationToolService(SimpleNamespace(enabled=True)))
    config = await chat._runtime_config.snapshot()
    inbound = InboundMessage(
        message_id="platform-id",
        event_type="message:test",
        scope_type=ScopeType.PRIVATE,
        sender=SenderIdentity("1001"),
        text="这个 work 结束了吗",
        bot_user_id="8001",
        source_event_id=17,
        person_id="person-1001",
        conversation_id="conversation-1",
    )
    runtime = ToolRuntime(
        inbound=inbound,
        gateway=None,
        allow_generic_onebot=False,
        allow_automation=True,
        actor_user_id="1001",
        runtime_config=config,
        conversation_key="query-test",
    )
    return chat, runtime


@pytest.mark.asyncio
async def test_queries_follow_automation_read_policy_not_mutation_admission(query_environment):
    chat, runtime = query_environment
    for read_only in (False, True):
        backend = MainAgentBackend(chat, replace(runtime, read_only=read_only))
        for action in ("get", "list"):
            name = f"automation_{action}"
            arguments = '{"automation_id":1}' if action == "get" else "{}"
            automation_allowed, _ = backend._ensure_capability_runtime().validate_call(
                name, arguments
            )
            assert automation_allowed
            assert backend.work_query_allowed(action)


@pytest.mark.asyncio
async def test_queries_require_trusted_internal_actor_and_automation_access(query_environment):
    chat, runtime = query_environment
    invalid = (
        replace(runtime, allow_automation=False),
        replace(runtime, inbound=replace(runtime.inbound, source_event_id=None)),
        replace(runtime, actor_user_id="another-user"),
        replace(runtime, inbound=None, actor_context=None),
    )
    for candidate in invalid:
        backend = MainAgentBackend(chat, candidate)
        assert not backend.work_query_allowed("get")
        assert not backend.work_query_allowed("list")


@pytest.mark.asyncio
async def test_delegated_query_scope_and_closed_profiles_are_enforced(query_environment):
    chat, runtime = query_environment
    delegated = MainAgentBackend(chat, runtime, allowed_tools=frozenset({"automation_get"}))
    assert delegated.work_query_allowed("get")
    assert not delegated.work_query_allowed("list")
    closed = MainAgentBackend(chat, replace(runtime, tools_closed=True))
    assert not closed.work_query_allowed("get")
    assert not closed.work_query_allowed("list")

    view = MemoryCapabilityView(
        eager_namespaces=(),
        requestable_namespaces=("memory.state.write",),
        hidden_namespaces=(),
        exclusive_namespace="memory.state.write",
        transition_revision=1,
    )
    memory = SimpleNamespace(exclusive_write=True, capability_view=lambda: view)
    exclusive = MainAgentBackend(chat, replace(runtime, memory_session=memory))
    for action in ("get", "list"):
        arguments = '{"automation_id":1}' if action == "get" else "{}"
        automation_allowed, _ = exclusive._ensure_capability_runtime().validate_call(
            f"automation_{action}", arguments
        )
        assert not automation_allowed
        assert not exclusive.work_query_allowed(action)


@pytest.mark.asyncio
async def test_self_directory_reads_do_not_require_or_borrow_a_person(query_environment):
    chat, base = query_environment
    actor = ToolActor(
        user_id="",
        bot_user_id="8001",
        group_id="2001",
        origin=TurnOrigin.SELF_INITIATIVE,
        instruction="查询工作状态",
        principal_kind="self",
        conversation_id="conversation-1",
        presence_id="presence-1",
        initiative_run_id="initiative-1",
    )
    runtime = replace(
        base,
        inbound=None,
        actor_user_id="",
        actor_context=actor,
        origin=TurnOrigin.SELF_INITIATIVE,
        current_group_id="2001",
        scope_type=ScopeType.GROUP,
        bot_user_id="8001",
        conversation_id="conversation-1",
        presence_id="presence-1",
        initiative_run_id="initiative-1",
    )
    backend = MainAgentBackend(chat, runtime)
    assert backend.work_query_allowed("get")
    assert backend.work_query_allowed("list")
    for invalid in (
        replace(runtime, allow_automation=False),
        replace(runtime, actor_user_id="1001"),
        replace(runtime, initiative_run_id="another-initiative"),
    ):
        refused = MainAgentBackend(chat, invalid)
        assert not refused.work_query_allowed("get")
        assert not refused.work_query_allowed("list")


@pytest.mark.asyncio
async def test_runner_query_gate_refuses_before_domain_read_and_keeps_lifecycle(query_environment):
    chat, tool_runtime = query_environment
    executed = []

    async def execute(name, arguments, call_key):
        executed.append((name, arguments, call_key))
        return json.dumps({"ok": True})

    control = SimpleNamespace(
        session=None,
        lease=SimpleNamespace(owner="query-owner"),
        execute=execute,
    )
    runtime = AgentRuntime(
        origin=TurnOrigin.USER_MESSAGE,
        actor_user_id="1001",
        actor_is_superuser=False,
        delegated_authority=None,
        conversation_key="query-test",
        current_group_id=None,
        bot_user_id="8001",
        gateway=None,
        runtime_config=tool_runtime.runtime_config,
        current_time=chat._time.current_default(),
        allowed_capabilities=frozenset(),
        max_tool_calls=8,
        max_model_requests=8,
        work_control=control,
    )

    async def dispatch(action, allowed):
        arguments = {"action": action}
        if action == "get":
            arguments["work_id"] = "original-work"
        call = ToolCall(action, ToolFunction("task_control", json.dumps(arguments)))
        backend = MainAgentBackend(chat, replace(tool_runtime, allow_automation=allowed))
        return await chat.runtime.runner._execute_tool_batch(
            (call,),
            backend,
            runtime,
            remaining_calls=8,
            max_parallel_calls=1,
            reusable_results={},
            cacheable_names=frozenset(),
            declared_names=frozenset({"task_control"}),
        )

    for action in ("get", "list"):
        refused = await dispatch(action, False)
        assert json.loads(refused.calls[0][1])["error"] == "work_query_not_authorized"
        assert refused.calls[0][2] is False
    assert executed == []
    for action in ("get", "list", "complete"):
        accepted = await dispatch(action, action != "complete")
        assert json.loads(accepted.calls[0][1])["ok"]
        assert accepted.calls[0][2] is True
        assert accepted.executed_count == 0
    assert [entry[1]["action"] for entry in executed] == ["get", "list", "complete"]
    assert executed[0][2] == "query-owner:get"
