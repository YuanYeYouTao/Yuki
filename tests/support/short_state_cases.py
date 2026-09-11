"""Cross-entrypoint working-state and actual request contract scenarios."""

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from qq_ai_bot.automation.handlers import _AutomationAgentBackend
from qq_ai_bot.automation.models import TurnOrigin
from qq_ai_bot.automation.registry import AutomationCapabilityRegistry
from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.domain.messages import ChatMessage, ChatResponse, ToolCall, ToolFunction
from qq_ai_bot.llm.fake import FakeLLMProvider
from qq_ai_bot.services.agent_runner import AgentRuntime
from qq_ai_bot.services.agent_tools import ToolRuntime
from qq_ai_bot.services.chat import _ChatAgentBackend
from qq_ai_bot.services.main_agent_contract import MainAgentContract
from qq_ai_bot.workspace.short_state import ShortState, encode
from qq_ai_bot.workspace.store import WorkspaceError, WorkspaceStore
from tests.conftest import build_harness, make_settings


async def run_short_state_cases(database, tmp_path, context):
    state = ShortState(WorkspaceStore(tmp_path / "working-state"))
    state.update({"slot": 1, "text": "想好的数字是73", "expected_revision": 0})
    assert ShortState(state.store).snapshot()[0]["text"] == "想好的数字是73"
    prefix = (
        ChatMessage(role="system", content="fixed"),
        ChatMessage(role="assistant", content="past"),
    )
    initial = (*prefix, ChatMessage(role="user", content="数字是什么"))
    frozen = await state.inject(initial)
    assert frozen[:2] == prefix
    assert "73" in frozen[-1].content
    concurrent = await asyncio.gather(
        *(
            state.execute(encode({"slot": 1, "text": text, "expected_revision": 1}))
            for text in ("数字73，已揭晓", "数字73，等下继续")
        )
    )
    assert sum(json.loads(result)["ok"] for result in concurrent) == 1
    assert frozen[-1].content != (await state.inject(initial))[-1].content
    before = state.snapshot()
    with pytest.raises(WorkspaceError, match="capacity"):
        state.update({"slot": 2, "text": "测" * 300, "expected_revision": 0})
    assert state.snapshot() == before
    assert len(encode(state.envelope(before)).encode()) <= 512
    for invalid in (
        {"slot": True, "text": "x", "expected_revision": 0},
        {"slot": 1, "text": "x", "expected_revision": False},
    ):
        assert not json.loads(await state.execute(encode(invalid)))["ok"]
    with state.store._transaction() as db:
        db.execute("UPDATE short_state SET expires_at=0")
    assert state.snapshot()[0]["text"] == ""
    assert not state.update({"slot": 1, "text": "stale", "expected_revision": 1})["ok"]

    provider = FakeLLMProvider()
    harness = build_harness(database, make_settings(database.url), provider)
    chat = harness.processor._chat
    from qq_ai_bot.social.automation import register_social_automation

    registry = AutomationCapabilityRegistry()
    register_social_automation(registry, {})
    contract = MainAgentContract(chat, SimpleNamespace(_registry=registry), state)
    chat._agent_runner.main_contract = contract
    chat._tools.short_state = state
    declared = await contract.definitions()
    revision = contract.revision
    assert len(revision) == 64
    copied = await contract.definitions()
    copied[0].parameters["injected"] = True
    assert await contract.definitions() == declared
    assert contract.revision == revision
    assert contract.automation_names["social.send_group_message"] == "send_group_message"
    assert contract.automation_names["workspace.write"] == "workspace_write"
    assert {"update_short_state", "call_onebot_api", "decline_reply", "set_reply_layout"} <= {
        t.name for t in declared
    }
    config = await chat._runtime_config.snapshot()
    runtime = AgentRuntime(
        origin=TurnOrigin.USER_MESSAGE,
        actor_user_id="10001",
        actor_is_superuser=False,
        delegated_authority=None,
        conversation_key="private:7777:10001",
        current_group_id=None,
        bot_user_id="7777",
        gateway=None,
        runtime_config=config,
        current_time=chat._time.current_default(),
        allowed_capabilities=frozenset(),
        max_tool_calls=3,
        max_model_requests=4,
    )
    for origin in (
        TurnOrigin.USER_MESSAGE,
        TurnOrigin.AUTONOMOUS_GROUP,
        TurnOrigin.PLUGIN_BACKGROUND,
    ):
        scoped = replace(runtime, origin=origin)
        backend = _ChatAgentBackend(
            chat,
            ToolRuntime(
                inbound=None,
                gateway=None,
                allow_generic_onebot=False,
                origin=origin,
                actor_user_id="10001",
                runtime_config=config,
                scope_type=ScopeType.PRIVATE,
                bot_user_id="7777",
                external_target_id="10001",
                read_only=True,
            ),
        )
        await chat._agent_runner.run(initial, scoped, backend)
        assert provider.requests[-1].tools == declared
        before_discovery = backend._capability_runtime.exposure_snapshot()
        before_exclusive = backend._capability_runtime.requested_exclusive_write()
        lookup = ToolCall(
            id="directory",
            function=ToolFunction(
                name="request_tools",
                arguments='{"query":"workspace list","max_results":4}',
            ),
        )
        backend.begin_batch((lookup,), scoped)
        discovered = json.loads(
            await backend.execute(
                lookup.function.name,
                lookup.function.arguments,
                scoped,
            )
        )
        assert "available_tools" in discovered["data"]
        assert "loaded_tools" not in discovered["data"]
        assert backend._capability_runtime.exposure_snapshot() == before_discovery
        assert backend._capability_runtime.requested_exclusive_write() == before_exclusive
        assert await contract.definitions() == declared
        # Global state has no person/group/origin ACL, including actorless and read-only turns.
        call = ToolCall(
            id="state",
            function=ToolFunction(
                name="update_short_state",
                arguments=encode(
                    {"slot": 1, "text": "73", "expected_revision": state.snapshot()[0]["revision"]}
                ),
            ),
        )
        backend.begin_batch((call,), scoped)
        assert json.loads(
            await backend.execute(call.function.name, call.function.arguments, scoped)
        )["ok"]
        denied = ToolCall(
            id="denied",
            function=ToolFunction(name="call_onebot_api", arguments='{"action":"x","params":{}}'),
        )
        backend.begin_batch((denied,), scoped)
        assert not json.loads(
            await backend.execute(denied.function.name, denied.function.arguments, scoped)
        )["ok"]
    automation = _AutomationAgentBackend(registry, context)
    automation.short_state = state
    automation.main_contract = contract
    await chat._agent_runner.run(
        initial, replace(runtime, origin=TurnOrigin.SCHEDULED_AUTOMATION), automation
    )
    assert provider.requests[-1].tools == declared
    assert "73" in provider.requests[-1].messages[-1].content
    await chat._agent_runner.run(initial, runtime, None)
    assert provider.requests[-1].tools == declared

    # Actual tool loop: group writes, private gets it; finalization retains the exact tool schemas.
    calls = 0

    def respond(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return ChatResponse(
                content="",
                latency_seconds=0,
                tool_calls=(
                    ToolCall(
                        id="write",
                        function=ToolFunction(
                            name="update_short_state",
                            arguments=encode(
                                {
                                    "slot": 1,
                                    "text": "想好的数字是91",
                                    "expected_revision": state.snapshot()[0]["revision"],
                                }
                            ),
                        ),
                    ),
                ),
            )
        return "想好了"

    provider._responder = respond
    start = len(provider.requests)
    await chat._agent_runner.run(initial, replace(runtime, max_model_requests=2), None)
    assert calls == 2
    assert provider.requests[start].tools == provider.requests[start + 1].tools == declared
    assert provider.requests[start].messages[-1] == provider.requests[start + 1].messages[2]
    assert provider.requests[start + 1].tool_choice == "none"
    provider._responder = lambda request: "91"
    await chat._agent_runner.run(initial, runtime, None)
    assert "91" in provider.requests[-1].messages[-1].content

    # State observations must refresh across requests even without local writes.
    # Exercise the shared runner with the actual frozen sandbox tool declaration.
    from qq_ai_bot.services.main_agent_contract import ShortStateOnlyBackend

    assert next(t for t in declared if t.name == "get_code_run").result_cacheable is False
    assert registry.require("sandbox.get_code_run").result_cacheable is False

    class ProgressBackend(ShortStateOnlyBackend):
        polls = 0

        def is_side_effecting(self, name, arguments_json, runtime):
            return False

        async def execute(self, name, arguments_json, runtime):
            assert name == "get_code_run"
            self.polls += 1
            return json.dumps(
                {
                    "ok": True,
                    "data": {
                        "status": "running" if self.polls <= 3 else "succeeded",
                        "pending": self.polls <= 3,
                        "artifacts": [] if self.polls <= 3 else ["downloaded-image"],
                    },
                }
            )

    progress = ProgressBackend(state)
    model_calls = 0

    def observe(request):
        nonlocal model_calls
        model_calls += 1
        results = [m for m in request.messages if m.role == "tool"]
        if results and json.loads(results[-1].content)["data"]["status"] == "succeeded":
            return "image ready"
        return ChatResponse(
            content="",
            latency_seconds=0,
            tool_calls=(
                ToolCall(
                    id=f"poll-{model_calls}",
                    function=ToolFunction(name="get_code_run", arguments='{"run_id":"same-job"}'),
                ),
            ),
        )

    provider._responder = observe
    result = await chat._agent_runner.run(
        initial, replace(runtime, max_tool_calls=5, max_model_requests=6), progress
    )
    assert result.text == "image ready"
    assert progress.polls == 4
    assert result.tool_calls_used == 4
    assert model_calls == 5

    # Real automation handler now composes through the main pipeline and shared
    # runner, without promoting its creator to a direct-message administrator.
    from qq_ai_bot.automation.handlers import AutomationCapabilityHandlers
    from qq_ai_bot.prompting.contracts import CORE_CONTRACT

    handlers = object.__new__(AutomationCapabilityHandlers)
    handlers._settings = harness.settings
    handlers._runtime_config = chat._runtime_config
    handlers._ledger = harness.ledger
    handlers._memories = SimpleNamespace()
    handlers._relationships = harness.relationships
    handlers._time = chat._time
    handlers._agent_runner = chat._agent_runner
    handlers._registry = registry
    handlers._gateway_factory = lambda context: None
    provider._responder = lambda request: "scheduled answer"
    generated = await handlers.generate(
        {
            "instruction": "scheduled work",
            "context_profile": "none",
            "max_characters": 200,
        },
        context,
    )
    assert generated.data["text"] == "scheduled answer"
    request = provider.requests[-1]
    assert request.tools == declared
    assert CORE_CONTRACT in request.messages[0].content
    assert "scheduled work" not in request.messages[0].content
    assert '"origin":"scheduled_automation"' in request.messages[-1].content
    assert "runtime.time" in request.messages[-1].content
    assert "runtime.short_state" in request.messages[-1].content
    assert "current_direct_event" not in request.messages[-1].content

    # Runtime registration cannot make a tool callable before the frozen manifest
    # changes, even when the backend would happily execute it.
    class UndeclaredBackend(ShortStateOnlyBackend):
        attempts = 0

        async def execute(self, name, arguments_json, runtime):
            self.attempts += 1
            return '{"ok":true}'

    late_backend = UndeclaredBackend(state)
    requests_seen = 0

    def undeclared_response(request):
        nonlocal requests_seen
        requests_seen += 1
        if requests_seen == 1:
            return ChatResponse(
                content="",
                latency_seconds=0,
                tool_calls=(
                    ToolCall(
                        id="late-tool-call",
                        function=ToolFunction(name="late_registered_tool", arguments="{}"),
                    ),
                ),
            )
        returned = next(m for m in request.messages if m.tool_call_id == "late-tool-call")
        assert json.loads(returned.content)["error"] == "tool_not_declared"
        return "not available in this deployment"

    provider._responder = undeclared_response
    await chat._agent_runner.run(initial, runtime, late_backend)
    assert late_backend.attempts == 0
