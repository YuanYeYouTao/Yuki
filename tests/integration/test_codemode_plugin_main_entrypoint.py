"""SDK main calls keep the original invocation owner across Code Mode segments."""

import asyncio
import hashlib
import json

import pytest
from sqlalchemy import select
from tests.conftest import build_harness, make_settings
from tests.support.codemode_cases import BINARY, requires_worker
from tests.support.social_identity_cases import social_env

from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.domain.messages import (
    ChatResponse,
    InboundMessage,
    SenderIdentity,
    ToolCall,
    ToolFunction,
)
from qq_ai_bot.llm.fake import FakeLLMProvider
from qq_ai_bot.persistence.models import ChatEventModel
from qq_ai_bot.plugin_host import main_turn
from qq_ai_bot.plugin_host.facades import HostPluginContext, PluginFacadeServices, PluginInvocation
from qq_ai_bot.runtime.origin import TurnOrigin
from qq_ai_bot.runtime.work_repository import WorkRepository
from qq_ai_bot.services.main_agent_contract import MainAgentContract
from qq_ai_bot.workspace.short_state import ShortState
from yuki_plugin_sdk.permissions import PluginPermission

pytestmark = requires_worker


@pytest.mark.parametrize("scenario", ["normal", "refused", "cancelled", "resumed"])
async def test_sdk_code_owner_reentry_and_original_source(database, tmp_path, scenario):
    env = await social_env(database, tmp_path)
    code = "await yuki_update_short_state({'slot': 1, 'text': 'SDK', 'expected_revision': 0})"
    if scenario == "refused":
        code = "await yuki_send_message({'text': 'MUST_NOT_SEND'})"
    if scenario == "cancelled":
        code += (
            "\nawait yuki_update_short_state({'slot': 1, 'text': 'LATE', 'expected_revision': 1})"
        )
    if scenario == "resumed":
        code = (
            "for i in range(36):\n"
            "    await yuki_update_short_state({'slot': 1, 'text': str(i), 'expected_revision': i})"
        )
    outputs = []

    def respond(request):
        if len(provider.requests) == 1:
            return ChatResponse(
                "",
                0,
                tool_calls=(
                    ToolCall("outer", ToolFunction("execute_code", json.dumps({"code": code}))),
                ),
            )
        paired = [m for m in request.messages if m.tool_call_id == "outer"]
        assert len(paired) == 1
        outputs.append(json.loads(paired[0].content))
        return "SDK_RESULT"

    provider = FakeLLMProvider(respond)
    settings = make_settings(
        database.url,
        runtime_work_enabled=True,
        code_mode_worker_path=BINARY,
        code_mode_worker_sha256=hashlib.sha256(BINARY.read_bytes()).hexdigest(),
    )
    chat = build_harness(database, settings, provider).processor._chat
    state = ShortState(env.store)
    chat.runtime.runner.main_contract = MainAgentContract(chat, state)
    chat.runtime.runner.code_mode_settings = settings
    host = HostPluginContext(
        plugin_id="test.code.sdk",
        approved_permissions=[PluginPermission.AGENT_RUN],
        services=PluginFacadeServices(
            ledger=chat._ledger,
            agent_runner=chat.runtime.runner,
            runtime_config=chat._runtime_config,
        ),
    )
    async with database.sessions() as reader:
        event = await reader.scalar(
            select(ChatEventModel).where(ChatEventModel.direction == "inbound")
        )
    inbound = InboundMessage(
        message_id=event.platform_message_id,
        source_event_id=event.id,
        event_type="message",
        scope_type=ScopeType.GROUP,
        sender=SenderIdentity("10001"),
        text="compute",
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
    downstream = tmp_path / "state-writes.jsonl"
    execute_state = state.execute

    async def record_state(arguments):
        result = await execute_state(arguments)
        if json.loads(result)["ok"]:
            with downstream.open("a") as output:
                output.write(arguments + "\n")
            if scenario == "cancelled":
                await WorkRepository(database).cancel(inbound.conversation_id)
        return result

    state.execute = record_state
    try:
        with host.bind(invocation):
            result = await host.agent.run("compute")
        identity = result.data["work_id"]
        row = await WorkRepository(database).get(identity)
        source = json.loads(row["source_json"])
        assert source["owner"] == "plugin_invocation"
        assert source["plugin_id"] == host.plugin_id
        assert source["trigger_event_id"] == event.id
        if scenario == "resumed":
            assert row["state"] == "queued"
            assert len(downstream.read_text().splitlines()) == 32
            await main_turn.resume_plugin_work(
                {host.plugin_id: host}.get, chat._ledger, row, source
            )
            tasks = tuple(main_turn._RUNNING.values())
            if tasks:
                await asyncio.wait_for(asyncio.gather(*tasks), 10)
        final = await host.agent.result(identity)
        assert final.data["state"] == ("cancelled" if scenario == "cancelled" else "completed")
        expected = {"normal": 1, "refused": 0, "cancelled": 1, "resumed": 36}[scenario]
        assert (len(downstream.read_text().splitlines()) if downstream.exists() else 0) == expected
        assert (state.snapshot()[0]["revision"] if state.snapshot() else 0) == expected
        before = len(provider.requests)
        with host.bind(invocation):
            repeated = await host.agent.run("compute")
        assert repeated.data["work_id"] == identity
        assert len(provider.requests) == before
        assert not any(action.startswith("send_") for action, _ in env.bot.calls)
        assert len(outputs) == int(scenario != "cancelled")
        if scenario == "refused":
            assert outputs[0]["operations"][0]["status"] == "not_executed"
    finally:
        await main_turn.close_plugin_main_tasks(host.plugin_id)
