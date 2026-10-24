"""The frozen Code wrapper reaches the original device domain and live permission gate."""

import json

import pytest
from sqlalchemy import func, select
from tests.unit.test_local_device_reintegration import admin as admin_fixture
from tests.unit.test_local_device_reintegration import app_env as app_fixture
from tests.unit.test_local_device_reintegration import grant
from tests.unit.test_local_device_reintegration import local as local_fixture

from yuki.capabilities.invocation import (
    Invocation,
    InvocationIdentity,
    TrustedInvocationContext,
    child_operation_id,
)
from yuki.codemode.tool_visibility import lookup_tools
from yuki.domain.conversations import ScopeType
from yuki.domain.messages import ToolCall, ToolFunction
from yuki.domain.tool_actor import ToolActor
from yuki.executor.db_models import ExecutorOperationModel
from yuki.plugin_host.facades import HostPluginContext
from yuki.runtime.origin import TurnOrigin
from yuki.services.agent_runner import AgentRuntime
from yuki.services.agent_tools import ToolRuntime
from yuki.services.main_agent_backend import MainAgentBackend
from yuki.services.main_agent_contract import MainAgentContract
from yuki_plugin_sdk.permissions import PluginPermission

admin = admin_fixture
app_env = app_fixture
local = local_fixture


@pytest.mark.parametrize("approved", [False, True])
async def test_code_device_wrapper_uses_original_child_identity_and_live_agent_run(
    local, monkeypatch, approved
):
    chat = local.env.harness.processor._chat
    chat._tools.local_executors = local.service
    runner = chat.runtime.runner
    contract = MainAgentContract(chat, runner.main_contract.state, code_enabled=True)
    monkeypatch.setattr(runner, "main_contract", contract)
    definitions = await contract.definitions()
    descriptor = next(tool for tool in definitions if tool.name == "local_device")
    api = contract.script_api
    assert api is not None
    assert api.tool_for("yuki_local_device") == "local_device"
    assert api.schemas["local_device"] == descriptor.parameters
    discovered = json.loads(lookup_tools(api, '{"name":"local_device"}'))
    assert discovered["data"]["script_name"] == "yuki_local_device"
    assert "execute_code" in {tool.name for tool in await contract.model_definitions()}
    await grant(local, [{"capability": "notify"}])
    channel = local.service.stream(local.executor["executor_id"])
    local.channels.append(channel)
    await anext(channel)

    config = await chat._runtime_config.snapshot()
    tool_runtime = ToolRuntime(
        inbound=None,
        actor_context=ToolActor(
            user_id="1001",
            bot_user_id="yuki-app",
            group_id=None,
            origin=TurnOrigin.USER_MESSAGE,
            instruction="use the explicitly authorized local device",
            execution_id="original-code-device-execution",
            person_id=local.person,
            conversation_id=local.principal.conversation_id,
            platform="app",
        ),
        person_id=local.person,
        scope_type=ScopeType.PRIVATE,
        external_target_id=local.person,
        conversation_id=local.principal.conversation_id,
        execution_id="original-code-device-execution",
        runtime_config=config,
    )

    class PluginScope:
        _closed = False
        _approved_permissions = frozenset({PluginPermission.AGENT_RUN}) if approved else frozenset()
        _require = HostPluginContext._require

        @staticmethod
        def _invocation(*, required):
            assert required is False
            return None

    backend = MainAgentBackend(chat, tool_runtime, plugin_host=PluginScope())
    await backend.prepare()
    runtime = AgentRuntime(
        origin=TurnOrigin.USER_MESSAGE,
        actor_user_id="1001",
        actor_is_superuser=False,
        conversation_key=f"conversation:{local.principal.conversation_id}",
        current_group_id=None,
        bot_user_id="yuki-app",
        gateway=None,
        runtime_config=config,
        allowed_capabilities=frozenset(),
        max_tool_calls=config.agent.max_tool_calls,
        max_model_requests=config.agent.max_model_requests,
        script_api=api,
        manifest_revision=contract.revision,
    )
    parent = "original-code-operation"
    child = child_operation_id(parent, 1)
    invocation = Invocation(
        InvocationIdentity(
            child,
            "original-code-device-execution",
            "original-code-chain",
            1,
            "c1",
            parent_operation_id=parent,
            child_ordinal=1,
        ),
        ToolCall(
            "c1",
            ToolFunction(
                api.tool_for("yuki_local_device"),
                json.dumps(
                    {
                        "action": "execute",
                        "executor_id": local.executor["executor_id"],
                        "capability": "notify",
                        "params": {"title": "Yuki", "body": "original Code child"},
                    }
                ),
            ),
        ),
        TrustedInvocationContext(runtime, contract.revision),
    )
    result = json.loads(await backend.execute_call(invocation))
    async with local.database.read_session() as reader:
        count = await reader.scalar(select(func.count()).select_from(ExecutorOperationModel))
    if not approved:
        assert result["ok"] is False and count == 0
    else:
        assert result["ok"] and result["data"]["pending"] and count == 1
        repeated = json.loads(await backend.execute_call(invocation))
        assert repeated["data"]["op_id"] == result["data"]["op_id"]
        async with local.database.read_session() as reader:
            operation = await reader.scalar(select(ExecutorOperationModel))
            assert operation.tool_call_key.endswith(child)
            assert (
                await reader.scalar(select(func.count()).select_from(ExecutorOperationModel)) == 1
            )
    assert await contract.definitions() == definitions
