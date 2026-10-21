"""Work reads use their actor and tree scope independently of Automation."""

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from tests.conftest import build_harness, make_settings

from yuki.automation.models import TurnOrigin
from yuki.automation.tools import AutomationToolService
from yuki.domain.conversations import ScopeType
from yuki.domain.messages import InboundMessage, SenderIdentity, ToolCall, ToolFunction
from yuki.domain.tool_actor import ToolActor
from yuki.services.agent_runner import AgentRuntime
from yuki.services.agent_tools import ToolRuntime
from yuki.services.main_agent_backend import MainAgentBackend


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
        runtime_config=config,
        conversation_key="query-test",
    )
    return chat, runtime


@pytest.mark.asyncio
async def test_queries_do_not_require_automation_or_mutation_admission(query_environment):
    chat, runtime = query_environment
    backend = MainAgentBackend(chat, runtime)
    for action in ("get", "list"):
        assert backend.work_query_allowed(action)


@pytest.mark.asyncio
async def test_queries_require_trusted_internal_actor(query_environment):
    chat, runtime = query_environment
    invalid = (
        replace(
            runtime, inbound=replace(runtime.inbound, source_event_id=None), actor_context=None
        ),
        replace(runtime, inbound=None, actor_context=None),
    )
    for candidate in invalid:
        backend = MainAgentBackend(chat, candidate)
        assert not backend.work_query_allowed("get")
        assert not backend.work_query_allowed("list")


@pytest.mark.asyncio
async def test_delegated_query_scope_and_closed_profiles_are_enforced(query_environment):
    chat, runtime = query_environment
    delegated = MainAgentBackend(chat, runtime)
    assert delegated.work_query_allowed("get")
    assert delegated.work_query_allowed("list")
    closed = MainAgentBackend(chat, replace(runtime, tools_closed=True))
    assert not closed.work_query_allowed("get")
    assert not closed.work_query_allowed("list")


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
        actor_context=actor,
        origin=TurnOrigin.SELF_INITIATIVE,
        scope_type=ScopeType.GROUP,
        conversation_id="conversation-1",
        initiative_run_id="initiative-1",
    )
    with pytest.raises(ValueError, match="invalid_self_tool_actor"):
        replace(actor, user_id="1001")
    backend = MainAgentBackend(chat, runtime)
    assert backend.work_query_allowed("get")
    assert backend.work_query_allowed("list")
    for invalid in (replace(runtime, initiative_run_id="another-initiative"),):
        refused = MainAgentBackend(chat, invalid)
        assert not refused.work_query_allowed("get")
        assert not refused.work_query_allowed("list")


@pytest.mark.asyncio
async def test_runner_query_gate_refuses_before_domain_read_and_keeps_lifecycle(query_environment):
    chat, tool_runtime = query_environment
    executed = []

    async def execute(name, arguments, call_key, *, invocation=None):
        executed.append((name, arguments, call_key))
        return json.dumps({"ok": True})

    control = SimpleNamespace(
        current=None,
        session=None,
        lease=SimpleNamespace(owner="query-owner"),
        accepted_ending=lambda call_key=None: None,
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
        backend = MainAgentBackend(chat, replace(tool_runtime, tools_closed=not allowed))
        return await chat.runtime.runner._execute_tool_batch(
            (call,),
            backend,
            runtime,
            remaining_calls=8,
            max_parallel_calls=1,
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
        assert accepted.calls[0][2] is (action == "complete")
        assert accepted.executed_count == 0
    assert [entry[1]["action"] for entry in executed] == ["get", "list", "complete"]
    assert executed[0][2] == "get"  # Read-only metadata keeps the original call ID.
