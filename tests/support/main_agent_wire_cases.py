"""Real entrypoint-to-HTTP comparison, without constructing model requests in tests."""

import json
from dataclasses import replace
from datetime import UTC, datetime
from itertools import pairwise
from unittest.mock import AsyncMock

import httpx

from qq_ai_bot.automation.handlers import AutomationCapabilityHandlers
from qq_ai_bot.automation.models import AutomationContext
from qq_ai_bot.automation.registry import build_capability_registry
from qq_ai_bot.conversation.scope import ConversationTurnSnapshot
from qq_ai_bot.domain.conversations import ConversationScope, ScopeType
from qq_ai_bot.domain.messages import InboundMessage, SenderIdentity
from qq_ai_bot.domain.profiles import UserProfileSnapshot
from qq_ai_bot.llm.deepseek_responses import DeepSeekResponsesProvider
from qq_ai_bot.llm.openai_compatible import OpenAICompatibleProvider
from qq_ai_bot.model_runtime.executor import TaskModelExecutor
from qq_ai_bot.model_runtime.models import (
    ModelCapability,
    ModelProfile,
    ModelProtocol,
    ModelRoute,
    ModelTask,
)
from qq_ai_bot.model_runtime.pool import ModelClientPool
from qq_ai_bot.model_runtime.profiles import ModelProfileCatalog
from qq_ai_bot.model_runtime.routes import ModelRouter
from qq_ai_bot.runtime.trigger import ExternalEventTurnTrigger
from qq_ai_bot.services.main_agent_contract import MainAgentContract
from qq_ai_bot.workspace.short_state import ShortState
from qq_ai_bot.workspace.store import WorkspaceStore
from tests.conftest import MemorySender, build_harness, make_settings


async def run_main_agent_wire_cases(database, tmp_path, automation_context):
    for protocol in (ModelProtocol.RESPONSES, ModelProtocol.CHAT_COMPLETIONS):
        await _run_protocol(database, tmp_path, automation_context, protocol)


async def _run_protocol(database, tmp_path, automation_context, protocol):
    state = ShortState(WorkspaceStore(tmp_path / f"wire-{protocol.value}"))
    state.update({"slot": 1, "text": "wire start", "expected_revision": 0})
    captured = {}
    current_entry = ""
    denied_name = ""

    def transport(request):
        payload = json.loads(request.content)
        chain = captured.setdefault(current_entry, [])
        chain.append(payload)
        first = len(chain) == 1
        denied = current_entry == "automation-generate" and len(chain) == 2
        call_id = f"state-{current_entry}"
        arguments = json.dumps(
            {
                "slot": 1,
                "text": f"done {current_entry}",
                "expected_revision": state.snapshot()[0]["revision"],
            }
        )
        tool_name = "update_short_state"
        if denied:
            call_id = "denied-send"
            tool_name = denied_name
            arguments = json.dumps({"user_id": "1001", "text": "must not send"})
        if protocol is ModelProtocol.RESPONSES:
            assert request.url.path == "/responses"
            output = (
                {
                    "type": "function_call",
                    "id": call_id,
                    "call_id": call_id,
                    "name": tool_name,
                    "arguments": arguments,
                    "status": "completed",
                }
                if first or denied
                else {
                    "type": "message",
                    "id": f"msg-{current_entry}",
                    "role": "assistant",
                    "status": "completed",
                    "content": [{"type": "output_text", "text": "已完成"}],
                }
            )
            response = {
                "id": f"response-{current_entry}-{len(chain)}",
                "status": "completed",
                "output": [output],
            }
        else:
            assert request.url.path == "/chat/completions"
            message = (
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {"name": tool_name, "arguments": arguments},
                        }
                    ],
                }
                if first or denied
                else {"role": "assistant", "content": "已完成"}
            )
            response = {
                "choices": [
                    {
                        "message": message,
                        "finish_reason": "tool_calls" if first or denied else "stop",
                    }
                ]
            }
        return httpx.Response(200, json=response)

    async with httpx.AsyncClient(
        base_url="https://wire.example", transport=httpx.MockTransport(transport)
    ) as client:
        provider_type = (
            DeepSeekResponsesProvider
            if protocol is ModelProtocol.RESPONSES
            else OpenAICompatibleProvider
        )
        provider = provider_type(
            base_url="https://wire.example",
            api_key="test",
            timeout_seconds=1,
            max_retries=0,
            client=client,
        )
        harness = build_harness(
            database,
            make_settings(database.url, automation_enabled=True, web_mode="tavily"),
            provider,
        )
        chat = harness.processor._chat
        profile = ModelProfile(
            id="wire",
            provider="deepseek",
            protocol=protocol,
            base_url="https://wire.example",
            api_key_env="WIRE_UNUSED",
            model="deepseek-v4-flash",
            timeout_seconds=1,
            max_retries=0,
            default_temperature=0.5,
            default_max_output_tokens=512,
            capabilities=frozenset(ModelCapability),
        )
        models = TaskModelExecutor(
            router=ModelRouter(
                ModelProfileCatalog(
                    profiles={"wire": profile},
                    routes={task: ModelRoute(task=task, profile_id="wire") for task in ModelTask},
                )
            ),
            pool=ModelClientPool(injected_profiles={"wire": provider}),
        )
        chat._agent_runner._models = models
        chat._models = models
        handlers = object.__new__(AutomationCapabilityHandlers)
        handlers._settings = harness.settings
        handlers._runtime_config = chat._runtime_config
        handlers._ledger = harness.ledger
        handlers._memories = chat._memories
        handlers._relationships = harness.relationships
        handlers._time = chat._time
        handlers._agent_runner = chat._agent_runner
        forbidden_send = AsyncMock(
            side_effect=AssertionError("pure generation reached send handler")
        )
        handlers._registry = build_capability_registry(
            {"onebot.send_private_message": forbidden_send}
        )
        handlers._gateway_factory = lambda context: None
        contract = MainAgentContract(chat, handlers, state)
        chat._agent_runner.main_contract = contract
        chat._tools.short_state = state
        manifest = await contract.definitions()
        denied_name = contract.automation_names["onebot.send_private_message"]
        assert len(manifest) > 10

        for name, user, group in (("private", "1001", None), ("admin-group", "9000", "2002")):
            current_entry = name
            message = InboundMessage(
                message_id=f"wire-{protocol.value}-{name}",
                event_type="message:test",
                scope_type=ScopeType.GROUP if group else ScopeType.PRIVATE,
                sender=SenderIdentity(user),
                text="记录这轮结果",
                bot_user_id="9999",
                group_id=group,
                mentions_bot=bool(group),
            )
            sender = MemorySender()
            result = await harness.processor.handle(message, sender)
            assert result.reason == "chat" and sender.messages

        for name in ("automation-generate", "automation-agent"):
            current_entry = name
            context = replace(
                automation_context,
                bot_user_id="9999",
                creator_user_id="1001",
                conversation_key=ConversationScope.private("9999", "1001").key,
                automation_context=AutomationContext(scene="creator_private", history_limit=3),
                authority=automation_context.authority.model_copy(
                    update={"bot_user_id": "9999", "actor_user_id": "1001"}
                ),
            )
            arguments = {"instruction": "记录这轮结果", "context_profile": "creator_private"}
            if name.endswith("generate"):
                result = await handlers.generate({**arguments, "max_characters": 200}, context)
            else:
                result = await handlers.agent(
                    {**arguments, "max_tool_calls": 3, "max_model_requests": 3}, context
                )
            assert result.data["text"] == "已完成"

        current_entry = "external-wakeup"
        scope = ConversationScope.private("9999", "1001")
        appended = await harness.scoped_events.append_external(
            scope=scope,
            platform_message_id=f"wire-{protocol.value}-external",
            source_plugin_id="wire-plugin",
            external_source="test",
            external_event_key=f"wire-{protocol.value}",
            external_event_type="completed",
            external_payload={"summary": "任务资料"},
            external_target_id="1001",
            content="任务资料",
            occurred_at=datetime.now(UTC),
        )
        token = await chat._turn_coordinator.begin_background(
            appended.scope.runtime_scope_key or appended.scope.scope.key
        )
        assert token is not None
        snapshot = ConversationTurnSnapshot(
            scope_id=appended.scope.id,
            scope_key=(appended.scope.runtime_scope_key or appended.scope.scope.key),
            generation=appended.scope.generation,
            trigger_event_id=appended.event.id,
            coordinator_version=token.version,
            transport_scope_key=scope.key,
        )
        result = await chat.generate_main_agent_wakeup(
            event=appended.event,
            trigger=ExternalEventTurnTrigger(
                plugin_id="wire-plugin",
                source_event_id=appended.event.id,
                target_type="private",
                target_id="1001",
                agent_intent="记录这轮结果",
            ),
            identity=scope,
            runtime=await chat._runtime_config.snapshot(),
            turn_token=token,
            turn_snapshot=snapshot,
            gateway=None,
            conversation_id=appended.event.canonical_conversation_id,
        )
        assert result.text == "已完成"

        current_entry = "autonomous-group"
        message = InboundMessage(
            message_id=f"wire-{protocol.value}-autonomous",
            event_type="message:test",
            scope_type=ScopeType.GROUP,
            sender=SenderIdentity("1001"),
            text="记录这轮结果",
            bot_user_id="9999",
            group_id="2002",
        )
        appended = await harness.scoped_events.append_inbound(message)
        token = await chat._turn_coordinator.notify_message(
            (appended.scope.runtime_scope_key or appended.scope.scope.key), observation=True
        )
        token = await chat._turn_coordinator.begin_autonomous(token)
        assert token is not None
        snapshot = ConversationTurnSnapshot(
            scope_id=appended.scope.id,
            scope_key=(appended.scope.runtime_scope_key or appended.scope.scope.key),
            generation=appended.scope.generation,
            trigger_event_id=appended.event.id,
            coordinator_version=token.version,
        )
        sender = MemorySender()
        await chat.respond(
            message,
            message.scope(),
            UserProfileSnapshot("1001", ScopeType.GROUP, group_id="2002"),
            message.text,
            sender,
            autonomous=True,
            turn_token=token,
            turn_snapshot=snapshot,
        )
        assert sender.messages

    forbidden_send.assert_not_awaited()
    assert len(captured) == 6
    fixed = None
    for name, chain in captured.items():
        assert len(chain) == (3 if name == "automation-generate" else 2), (
            protocol,
            name,
            len(chain),
        )
        first, second = chain[0], chain[-1]
        assert all(item["tools"] == first["tools"] for item in chain)
        assert len(first["tools"]) == len(manifest)
        if protocol is ModelProtocol.RESPONSES:
            assert second["instructions"] == first["instructions"]
            old, new = first["input"], second["input"]
            outputs = [item["output"] for item in new if item.get("type") == "function_call_output"]
        else:
            old, new = first["messages"], second["messages"]
            outputs = [item["content"] for item in new if item.get("role") == "tool"]
        assert new[: len(old)] == old
        assert len(outputs) == (2 if name == "automation-generate" else 1)
        assert json.loads(outputs[0])["ok"] is True
        if name == "automation-generate":
            assert json.loads(outputs[-1]) == {"ok": False, "error": "capability_not_allowed"}
        sequence_key = "input" if protocol is ModelProtocol.RESPONSES else "messages"
        for previous, following in pairwise(chain):
            assert following[sequence_key][: len(previous[sequence_key])] == previous[sequence_key]
        # Preserve mapping insertion order as well as array order: schema key
        # reordering changes the serialized prefix even when dict equality passes.
        fixed_payloads = []
        for payload in chain:
            static = {
                key: value
                for key, value in payload.items()
                if key not in {sequence_key, "tool_choice"}
            }
            if protocol is ModelProtocol.CHAT_COMPLETIONS:
                static["instructions"] = payload["messages"][0]
            fixed_payloads.append(json.dumps(static, ensure_ascii=False, separators=(",", ":")))
        assert all(item == fixed_payloads[0] for item in fixed_payloads)
        shape = fixed_payloads[0]
        assert fixed is None or shape == fixed, (protocol, name)
        fixed = shape
