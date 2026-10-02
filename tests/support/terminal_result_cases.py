"""Exercise oversized terminal output through the real chat tool backend."""

import json
from dataclasses import replace
from types import SimpleNamespace

from sqlalchemy import select

from qq_ai_bot.automation.models import TurnOrigin
from qq_ai_bot.conversation.scope import ConversationTurnSnapshot
from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.domain.messages import InboundMessage, SenderIdentity, ToolCall, ToolFunction
from qq_ai_bot.mcp.repository import ToolArtifactRepository
from qq_ai_bot.persistence.models import ChatEventModel
from qq_ai_bot.services.agent_runner import AgentRuntime
from qq_ai_bot.services.agent_tools import ToolRuntime
from qq_ai_bot.services.main_agent_backend import MainAgentBackend
from tests.conftest import build_harness, make_settings
from tests.support.social_identity_cases import social_env


async def check_terminal_result_recovery(database, tmp_path):
    env = await social_env(database, tmp_path)
    async with database.sessions() as reader:
        event = await reader.scalar(
            select(ChatEventModel).where(
                ChatEventModel.canonical_conversation_id == env.context.conversation_id
            )
        )
    inbound = InboundMessage(
        "inbound",
        "message",
        ScopeType.GROUP,
        SenderIdentity("10001"),
        "terminal result test",
        bot_user_id="80001",
        group_id="20001",
        person_id=env.person,
        space_id=env.space,
        conversation_id=env.context.conversation_id,
        presence_id=env.presence,
        source_event_id=event.id,
    )
    output = ('中文\\"quoted"\n' * 3000) + "END"
    source = dict(
        run_id="receipt-123",
        status="running",
        pending=True,
        output=output,
        cursor=0,
        next_cursor=len(output.encode()),
        output_offset=len(output.encode()),
        output_lost=False,
        truncated=False,
        exit_code=None,
    )
    calls = []

    async def execute(name, arguments, **kwargs):
        calls.append(name)
        return dict(source)

    for enabled in (True, False):
        harness = build_harness(database, make_settings(database.url))
        chat = harness.processor._chat
        state = await harness.conversation_scopes.get(inbound.scope())
        token = await chat._turn_coordinator.notify_message(state.runtime_scope_key)
        artifacts = ToolArtifactRepository(
            database, tmp_path / "terminal-artifacts", retention_seconds=60
        )
        chat._tool_artifacts = artifacts
        chat._tools.sandbox_client = SimpleNamespace(execute=execute)
        snapshot = await chat._runtime_config.snapshot()
        snapshot = replace(
            snapshot, tooling=replace(snapshot.tooling, result_artifact_enabled=enabled)
        )
        tool_runtime = ToolRuntime(
            execution_id="terminal-result-test",
            inbound=inbound,
            gateway=None,
            allow_generic_onebot=False,
            runtime_config=snapshot,
            actor_user_id="10001",
            current_group_id="20001",
            turn_snapshot=ConversationTurnSnapshot(
                state.id, state.runtime_scope_key, state.generation, event.id, token.version
            ),
        )
        runtime = AgentRuntime(
            origin=TurnOrigin.USER_MESSAGE,
            actor_user_id="10001",
            actor_is_superuser=False,
            delegated_authority=None,
            conversation_key=inbound.scope().key,
            current_group_id="20001",
            bot_user_id="80001",
            gateway=None,
            runtime_config=snapshot,
            current_time=chat._time.current_default(),
            allowed_capabilities=frozenset(),
            max_tool_calls=4,
            max_model_requests=5,
        )
        backend = MainAgentBackend(chat, tool_runtime)
        backend.definitions(runtime, web_was_used=False)
        for index, (name, args) in enumerate(
            (
                ("terminal_exec", {"command": "echo test"}),
                ("terminal_read", {"run_id": "receipt-123", "cursor": 0}),
            )
        ):
            call = ToolCall(
                id=f"call-{index}", function=ToolFunction(name=name, arguments=json.dumps(args))
            )
            backend.begin_batch((call,), runtime)
            text = await backend.execute(name, call.function.arguments, runtime)
            payload = json.loads(text)
            assert payload["ok"], payload
            assert len(text) <= snapshot.agent.tool_result_max_characters
            assert payload["progress"]["run_id"] == "receipt-123"
            assert payload["progress"]["next_cursor"] == source["next_cursor"]
            assert payload["progress"]["preview_truncated"]
            assert not backend._tools_closed
            assert backend.finalize("task still running", runtime) == "task still running"
            if enabled:
                handle = payload["artifact_handle"]
                chunks, offset = [], 0
                while True:
                    page = await artifacts.read(handle, operation="text", offset=offset, limit=1500)
                    assert page is not None, handle
                    chunks.append(page["content"])
                    if page["next_offset"] is None:
                        break
                    offset = page["next_offset"]
                restored = json.loads("".join(chunks))
                assert restored["data"]["output"] == output
                assert restored["mutation_committed"] is (name == "terminal_exec")
        assert calls[-2:] == ["terminal_exec", "terminal_read"]

        from qq_ai_bot.workspace.store import WorkspaceError

        async def import_attachment(name, args, **kwargs):
            if "event_id" not in args:
                raise WorkspaceError("attachment_not_found")
            return {"path": "/workspace/imported.png", "artifact_id": "imported-file"}

        chat._tools.workspace_service = SimpleNamespace(execute=import_attachment)
        for index, args in enumerate(
            (
                {"attachment_index": 0, "destination": "imported.png"},
                {"event_id": 123, "attachment_index": 0, "destination": "imported.png"},
            )
        ):
            call = ToolCall(
                id=f"import-{index}",
                function=ToolFunction(
                    name="save_conversation_attachment_to_workspace", arguments=json.dumps(args)
                ),
            )
            backend.begin_batch((call,), runtime)
            payload = json.loads(
                await backend.execute(call.function.name, call.function.arguments, runtime)
            )
            assert payload["ok"] is bool(index), payload
            assert not backend._tools_closed
            assert backend.finalize("can continue", runtime) == "can continue"
            if not index:
                assert payload["error"] == "tool_input_validation_failed", payload

        # The fixture now has an authenticated inbound sender, so the normal
        # explicit-send reminder applies; it must not alter the internal result.
        assert backend.response_feedback("定时任务已经创建", runtime) is not None
        assert backend.finalize("定时任务已经创建", runtime) == "定时任务已经创建"
