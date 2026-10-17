"""New model reads reach the real authorization and handler after earlier reads."""

import json
from unittest.mock import AsyncMock

import pytest
from tests.conftest import MemorySender, build_harness, make_settings
from tests.integration.test_plugin_frozen_binding import write_plugin

from qq_ai_bot import __version__
from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.domain.messages import (
    ChatMessage,
    ChatResponse,
    InboundMessage,
    SenderIdentity,
    ToolCall,
    ToolFunction,
)
from qq_ai_bot.llm.fake import FakeLLMProvider
from qq_ai_bot.plugin_host.capability_adapter import PluginCapabilityAdapter
from qq_ai_bot.plugin_host.discovery import PluginDiscovery
from qq_ai_bot.plugin_host.event_bus import PluginEventBus
from qq_ai_bot.plugin_host.extension_registry import ExtensionKind, ExtensionRegistry
from qq_ai_bot.plugin_host.facades import HostPluginContext, PluginFacadeServices
from qq_ai_bot.plugin_host.loader import PluginLoader
from qq_ai_bot.plugin_host.manager import PluginManager
from qq_ai_bot.plugin_host.repository import PluginInstallationRepository
from qq_ai_bot.services.main_agent_contract import MainAgentContract
from qq_ai_bot.workspace.short_state import ShortState
from qq_ai_bot.workspace.store import WorkspaceStore


@pytest.mark.parametrize("same_response", [False, True])
async def test_runner_does_not_reuse_a_different_read_call(database, tmp_path, same_response):
    write_plugin(tmp_path / "plugins" / "test.contract", 1, "READ", "value")
    registry = ExtensionRegistry()
    installations = PluginInstallationRepository(database)
    contexts, executed, returned = {}, [], []

    class FixtureContext(HostPluginContext):
        pass

    def context_factory(manifest, permissions):
        context = FixtureContext(
            plugin_id=manifest.id,
            approved_permissions=permissions,
            services=PluginFacadeServices(approval_revision=manifest.manifest_hash),
        )
        context.calls = executed
        contexts[manifest.id] = context
        return context

    manager = PluginManager(
        enabled=True,
        discovery=PluginDiscovery(tmp_path / "plugins", yuki_version=__version__),
        installations=installations,
        loader=PluginLoader(),
        extensions=registry,
        event_bus=PluginEventBus(),
        context_factory=context_factory,
    )
    await manager.start()
    try:
        await manager.approve("test.contract", actor_user_id="9000")
        await manager.enable("test.contract", actor_user_id="9000")
        name = registry.list(kind=ExtensionKind.TOOL)[0].model_name
        calls = 0

        def respond(request):
            nonlocal calls
            calls += 1
            results = [json.loads(item.content) for item in request.messages if item.role == "tool"]
            if calls > 1:
                returned[:] = results
            if calls == 1 or (calls == 2 and not same_response):
                return ChatResponse(
                    "",
                    0,
                    tool_calls=tuple(
                        ToolCall(f"read-{calls}-{index}", ToolFunction(name, '{"value":"same"}'))
                        for index in range(2 if same_response else 1)
                    ),
                )
            return "NO_REPLY"

        provider = FakeLLMProvider(respond)
        complete = provider.complete

        async def revoke_before_next_response(request):
            if not same_response and len(provider.requests) == 1:
                await manager.disable("test.contract", actor_user_id="9000")
            return await complete(request)

        provider.complete = revoke_before_next_response
        harness = build_harness(database, make_settings(database.url), provider)
        chat = harness.processor._chat
        chat.set_plugin_tools(
            PluginCapabilityAdapter(
                registry=registry,
                installations=installations,
                is_running=lambda plugin: plugin in manager.running_plugin_ids,
                invocation_scope=lambda plugin, runtime: contexts[plugin].invocation_scope(
                    plugin, runtime
                ),
            )
        )
        contract = MainAgentContract(chat, ShortState(WorkspaceStore(tmp_path / "workspace")))
        chat.runtime.runner.main_contract = contract
        await contract.definitions()
        frozen = contract.revision
        result = await harness.processor.handle(
            InboundMessage(
                message_id="read-original",
                event_type="message:test",
                scope_type=ScopeType.GROUP,
                sender=SenderIdentity("1001"),
                text="read the current fixture",
                bot_user_id="8000",
                group_id="2001",
                mentions_bot=True,
            ),
            MemorySender(),
        )
        assert result.reason == "chat"
        assert contract.revision == frozen
        if same_response:
            assert len(executed) == 2 and len(returned) == 2
            assert all(item["ok"] for item in returned)
        else:
            assert len(executed) == 1 and len(returned) == 2
            assert returned[0]["ok"]
            assert not returned[-1]["ok"]
            assert returned[-1]["error_code"] in {
                "plugin_tool_contract_changed",
                "plugin_tool_denied",
            }
    finally:
        await manager.stop()


async def test_saved_legacy_readonly_alias_restores_original_receipt_without_dispatch(database):
    from tests.support.work_session import WorkSession, invoke_tool
    from tests.unit.test_work_journal_source_retry import _session

    from qq_ai_bot.services.turn_transcript import TurnTranscript

    control, session, _selected, _unselected = await _session(database)
    original = ToolCall("original-read", ToolFunction("read_fixture", '{"query":"same"}'))
    session.transcript.append(ChatMessage("assistant", "", tool_calls=(original,)))
    read = AsyncMock(return_value='{"ok":true,"data":{"original":"paid fact"}}')
    result = await invoke_tool(session, original, read, side_effecting=False)
    session.transcript.append_result(original.id, result)
    await session.save("paired")
    original_key = session.call_key(original.id)
    session.sequence += 1
    alias = ToolCall("old-saved-alias", original.function)
    session.transcript.append(ChatMessage("assistant", "", tool_calls=(alias,)))
    session.pending_readonly_keys[alias.id] = original_key
    await session.save("response", (alias,))

    restored = WorkSession(control, session.contract)
    control.session = restored
    await restored.restore(TurnTranscript((ChatMessage("user", "original task"),)))
    assert not restored.pending
    observation = next(
        json.loads(item.content)
        for item in restored.transcript.entries()
        if isinstance(item, ChatMessage)
        and item.role == "user"
        and item.content
        and '"kind": "work_unobserved_tool_round"' in item.content
    )
    assert observation["calls"][0]["call_id"] == alias.id
    assert observation["calls"][0]["result"] == result
    read.assert_awaited_once()
