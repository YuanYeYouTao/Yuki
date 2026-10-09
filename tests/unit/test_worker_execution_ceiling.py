"""A child's frozen tool contract is enforced by the real backend, without a wrapper.

Every path calls MainAgentBackend or the shared runner directly, so a removed
worker wrapper cannot be what refuses: direct, Code, lifecycle control, Work
query, lookup and native over-reach never reach a handler.
"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from tests.conftest import build_harness, make_settings
from tests.support.social_identity_cases import social_env
from tests.unit.test_capability_runtime_security import _runtime

from qq_ai_bot.capabilities import (
    CapabilityTrustSource,
    InProcessToolProvider,
    ToolProviderRegistry,
)
from qq_ai_bot.capabilities.invocation import direct_invocations
from qq_ai_bot.capabilities.results import ToolExecutionResult
from qq_ai_bot.codemode.api_projection import project
from qq_ai_bot.domain.messages import ChatTool, ToolCall, ToolFunction
from qq_ai_bot.runtime.subagent_repository import SubagentRepository
from qq_ai_bot.runtime.work_activation import current_work_control
from qq_ai_bot.runtime.work_control import WorkControl
from qq_ai_bot.runtime.work_repository import WorkRepository
from qq_ai_bot.services.agent_tools import ToolRuntime
from qq_ai_bot.services.main_agent_backend import MainAgentBackend

WORKER = frozenset({"search_memory", "task_control"})
OVER_REACH = ("send_message", "memory_change", "automation_list", "subagent_start")
WORKER_REFUSAL = {"ok": False, "error": "worker_tool_not_declared"}


def _schema() -> dict[str, object]:
    return {"type": "object", "properties": {}, "additionalProperties": False}


@pytest.fixture
async def child(database, tmp_path, monkeypatch):
    env = await social_env(database, tmp_path)
    repository = WorkRepository(database)
    root_lease = await repository.acquire(env.context.conversation_id, 1)
    source = {"actor_user_id": "10001", "origin": "user_message"}
    root = await repository.accept(root_lease, source_key="root", source=source, goal="root")
    children = SubagentRepository(repository)
    identity = await children.start(
        root_lease, root["id"], "child", {"goal": "read", "output_kind": "answer"}
    )
    sibling = await children.start(
        root_lease, root["id"], "sibling", {"goal": "other", "output_kind": "answer"}
    )
    lease = await children.acquire(identity)
    row = await repository.get(identity)
    control = WorkControl(repository, lease, "child", json.loads(row["source_json"]), AsyncMock())
    control.current = row
    executed_controls: list[str] = []
    original_execute = WorkControl.execute

    async def counted(self, name, args, call_key):
        executed_controls.append(name)
        return await original_execute(self, name, args, call_key)

    monkeypatch.setattr(WorkControl, "execute", counted)

    chat = build_harness(database, make_settings(database.url)).processor._chat
    handled: list[str] = []

    async def dispatch(name, arguments, runtime):
        handled.append(name)
        return ToolExecutionResult(provider_id="core", tool_name=name, ok=True, data={})

    registry = ToolProviderRegistry()
    registry.register(
        InProcessToolProvider(
            provider_id="core",
            source=CapabilityTrustSource.CORE,
            definitions=lambda _: tuple(
                ChatTool(name, name, _schema()) for name in ("search_memory", *OVER_REACH)
            ),
            execute=dispatch,
        )
    )
    tool_runtime = ToolRuntime(
        inbound=None,
        gateway=None,
        allow_generic_onebot=False,
        runtime_config=await chat._runtime_config.snapshot(),
    )
    backend = MainAgentBackend(chat, tool_runtime, allowed_tools=WORKER)
    backend._catalog = registry.catalog(tool_runtime)
    backend._capability_runtime = _runtime(*backend._catalog.entries)
    backend._capability_runtime.initial_exposure()
    backend._callable_tool_names = set(backend._capability_runtime.callable_capability_ids())
    runtime = SimpleNamespace(
        work_control=control,
        origin=tool_runtime.origin,
        delegated_authority=None,
        script_api=project(
            tuple(ChatTool(name, name, _schema()) for name in sorted(WORKER)), "worker"
        ),
        runtime_config=tool_runtime.runtime_config,
        max_tool_calls=8,
        execution_id=identity,
    )
    token = current_work_control.set(control)
    try:
        yield SimpleNamespace(
            chat=chat,
            backend=backend,
            runtime=runtime,
            control=control,
            controls=executed_controls,
            handled=handled,
            identity=identity,
            root=root["id"],
            sibling=sibling,
        )
    finally:
        current_work_control.reset(token)


@pytest.mark.parametrize("name", OVER_REACH)
async def test_direct_and_code_child_over_reach_never_reach_a_handler(child, name):
    call = ToolCall("call", ToolFunction(name, "{}"))
    invocation = direct_invocations((call,), child.runtime)[0]
    assert json.loads(await child.backend.execute_call(invocation)) == WORKER_REFUSAL
    host = child.chat.runtime.runner._code_host(
        child.backend, child.runtime, declared_names=WORKER, max_parallel_calls=1
    )
    refused = json.loads(await host.execute_business(invocation, False))
    assert refused["error"] == "tool_not_declared"
    assert child.handled == []


def test_child_catalog_is_its_frozen_subset_and_root_declaration_is_unchanged(child):
    names = {tool.name for tool in child.backend.definitions(child.runtime, web_was_used=False)}
    assert names == {"search_memory"}
    token = current_work_control.set(None)
    try:
        root = {
            tool.name
            for tool in child.backend.definitions(
                SimpleNamespace(work_control=None), web_was_used=False
            )
        }
    finally:
        current_work_control.reset(token)
    # Root/plugin declarations are not narrowed by a ceiling.
    assert root == {tool.name for tool in child.backend._capability_runtime.definitions()}
    assert "send_message" in root and "memory_change" in root


async def test_child_lifecycle_spawn_and_control_over_reach_never_execute(child):
    runner = child.chat.runtime.runner
    declared = WORKER | {"subagent_start", "subagent_control"}
    for name in ("subagent_start", "subagent_control"):
        call = ToolCall("ctl", ToolFunction(name, '{"goal":"x"}'))
        result, executed = await runner._execute_control_call(
            call, child.backend, child.runtime, declared, "key"
        )
        assert not executed and json.loads(result)["error"] == "work_control_unavailable"
    assert child.controls == []


async def test_child_query_reads_only_its_own_work_without_automation_authority(child):
    runner = child.chat.runtime.runner
    assert not child.backend._runtime.allow_automation

    async def get(work_id):
        call = ToolCall(
            "get", ToolFunction("task_control", json.dumps({"action": "get", "work_id": work_id}))
        )
        result, _ = await runner._execute_control_call(
            call, child.backend, child.runtime, WORKER, f"get:{work_id}"
        )
        return json.loads(result)

    assert (await get(child.identity))["work"]["work_id"] == child.identity
    for other in (child.root, child.sibling):
        assert (await get(other))["error"] == "work_not_found_or_not_authorized"
    # Without task_control in its contract the child has no query at all.
    narrowed = MainAgentBackend(
        child.chat, child.backend._runtime, allowed_tools=frozenset({"search_memory"})
    )
    call = ToolCall("q", ToolFunction("task_control", '{"action":"list"}'))
    result, executed = await runner._execute_control_call(
        call, narrowed, child.runtime, WORKER, "list"
    )
    assert not executed and json.loads(result)["error"] == "work_control_unavailable"
    assert child.controls == ["task_control"] * 3


async def test_root_query_still_requires_automation_read_authority(child):
    token = current_work_control.set(None)
    try:
        root = MainAgentBackend(child.chat, child.backend._runtime)
        assert not root.work_query_allowed("get") and not root.work_query_allowed("list")
    finally:
        current_work_control.reset(token)


async def test_missing_child_api_fails_closed_for_code_and_lookup(child):
    runner = child.chat.runtime.runner
    runner.main_contract = SimpleNamespace(
        script_api=project(
            tuple(ChatTool(name, name, _schema()) for name in ("search_memory", *OVER_REACH)),
            "main",
        ),
        revision="main",
        plugin_binding_current=lambda _name: True,
    )
    child.runtime.script_api = None
    host = runner._code_host(
        child.backend, child.runtime, declared_names=WORKER, max_parallel_calls=1
    )
    assert host.api is None
    call = ToolCall("call", ToolFunction("send_message", "{}"))
    invocation = direct_invocations((call,), child.runtime)[0]
    assert json.loads(await host.execute_business(invocation, False))["error"] == (
        "tool_not_declared"
    )
    lookup = ToolCall("lookup", ToolFunction("lookup_tools", '{"query":"send"}'))
    for calls in (
        (lookup,),
        (lookup, ToolCall("q", ToolFunction("task_control", '{"action":"list"}'))),
    ):
        result = await runner._execute_tool_batch(
            calls,
            child.backend,
            child.runtime,
            remaining_calls=8,
            max_parallel_calls=1,
            reusable_results={},
            cacheable_names=frozenset(),
            declared_names=WORKER | {"lookup_tools"},
        )
        lookup_result = json.loads(result.calls[0][1])
        assert lookup_result["ok"] is False
        assert lookup_result["error"] == "tool_catalog_unavailable"
    assert child.handled == []


async def test_native_tools_need_the_child_allowed_capabilities(child):
    runner = child.chat.runtime.runner
    _, native = runner.prepare_request_tools(
        (),
        runtime_config=child.runtime.runtime_config,
        allowed_capabilities=frozenset(),
        web_was_used=False,
    )
    assert native == ()
