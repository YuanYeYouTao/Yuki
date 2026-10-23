"""Correctable Main tool arguments retain the original native dispatch and receipt."""

import json

import pytest
from sqlalchemy import select
from tests.conftest import make_settings
from tests.integration.test_external_agent_bridge import NativeComputer, accepted_work, directory
from tests.integration.test_external_agent_bridge import bridge as bridge_fixture
from tests.support.service_dependencies import tool_dependencies

from yuki.capabilities.invocation import ToolInvocationContext, current_invocation
from yuki.domain.conversations import ScopeType
from yuki.domain.tool_actor import ToolActor
from yuki.memory.repository import MemoryFactRepository
from yuki.memory.service import MemoryFactService
from yuki.persistence.models import ChatEventModel
from yuki.persistence.repositories import EventLedgerRepository
from yuki.runtime.origin import TurnOrigin
from yuki.services.agent_tools import AgentToolService, ToolRuntime

bridge = bridge_fixture


@pytest.mark.parametrize("reference", ["operation_id", "run_id"])
async def test_main_argument_errors_can_be_corrected_without_duplicate_native_dispatch(
    bridge, reference
):
    settings = make_settings(bridge.database.url, runtime_work_enabled=True)
    tools = AgentToolService(
        settings=settings,
        ledger=EventLedgerRepository(bridge.database),
        memories=MemoryFactService(MemoryFactRepository(bridge.database)),
        **tool_dependencies(bridge.database, settings),
    )
    tools.external_agent_service = bridge.service
    async with bridge.database.read_session() as reader:
        event = await reader.scalar(select(ChatEventModel))
    runtime = ToolRuntime(
        inbound=None,
        scope_type=ScopeType.PRIVATE,
        actor_context=ToolActor(
            user_id="10001",
            bot_user_id=bridge.env.bot.self_id,
            group_id=None,
            origin=TurnOrigin.USER_MESSAGE,
            instruction="Delegate the selected project",
            person_id=bridge.env.person,
            conversation_id=bridge.env.context.conversation_id,
            presence_id=bridge.env.presence,
            event_id=event.id,
        ),
        conversation_id=bridge.env.context.conversation_id,
        trigger_event_id=event.id,
        execution_id=f"event:{event.id}",
    )

    async def invoke(arguments, invocation_key):
        token = current_invocation.set(ToolInvocationContext(runtime, invocation_key))
        try:
            return await tools.execute("external_agent", json.dumps(arguments), runtime)
        finally:
            current_invocation.reset(token)

    computer = await NativeComputer(bridge).start()
    try:
        instance, session = await directory(bridge)
        prompt = {
            "action": "prompt",
            "instance_id": instance,
            "session_id": session,
            "prompt": "test",
        }
        denied = await invoke(prompt, "original-prompt")
        assert denied.error_code == "accept_work_before_execution"
        assert "task_control(action=accept)" in denied.public_message
        assert not any(command["action"] == "prompt" for command in computer.commands)
        async with accepted_work(bridge):
            original = await invoke(prompt, "original-prompt")
            assert original.ok
            replay = await invoke(prompt, "original-prompt")
            assert replay.data == original.data
            receipt = original.data
            await computer.event(
                receipt["operation_id"],
                "permission",
                {"native_request_id": 0, "connection_id": "original-native-connection"},
            )
            observed = await invoke(
                {"action": "get", "operation_id": receipt["operation_id"]}, "read-original"
            )
            permission = observed.data["events"][0]["permission_id"]
            response = {
                "action": "respond",
                "instance_id": instance,
                "execution_id": receipt["execution_id"],
                "permission_id": permission,
                "option_id": "accept",
            }
            missing = await invoke(response, "original-response")
            assert missing.error_code == "operation_id_required"
            assert "operation_id" in missing.public_message and "run_id" in missing.public_message
            assert not any(command["action"] == "respond" for command in computer.commands)
            response[reference] = receipt[reference]
            answered = await invoke(response, "original-response")
            assert answered.ok
            repeated = await invoke(response, "original-response")
            assert repeated.data == answered.data
        assert [command["action"] for command in computer.commands].count("prompt") == 1
        assert [command["action"] for command in computer.commands].count("respond") == 1
        native = next(command for command in computer.commands if command["action"] == "respond")
        assert native["arguments"]["native_request_id"] == 0
        assert native["arguments"]["operation_id"] == receipt["operation_id"]
    finally:
        await computer.close()
