"""A real inbound source reaches the shared core; short chat remains Work-free."""

import hashlib
import json

import pytest
from sqlalchemy import select
from tests.conftest import MemorySender, build_harness, make_settings
from tests.support.codemode_cases import BINARY, requires_worker
from tests.support.runtime_execution import make_work_resumer
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
from qq_ai_bot.runtime.work_repository import WorkRepository
from qq_ai_bot.runtime.work_schema_v1 import work
from qq_ai_bot.services.main_agent_contract import MainAgentContract
from qq_ai_bot.workspace.short_state import ShortState

pytestmark = requires_worker


@pytest.mark.parametrize("scenario", ["short", "normal", "refused", "cancelled", "resumed"])
async def test_chat_code_requires_original_event_admission_and_resumes_same_work(
    database, tmp_path, scenario
):
    env = await social_env(database, tmp_path)
    count = 36 if scenario == "resumed" else 1
    code = f"for i in range({count}):\n    await yuki_get_my_capabilities({{'mode': 'summary'}})"
    if scenario == "cancelled":
        code += "\nawait yuki_get_my_capabilities({'mode': 'summary'})"
    outputs = []

    def tool(name, arguments, identity):
        return ChatResponse(
            "", 0, tool_calls=(ToolCall(identity, ToolFunction(name, json.dumps(arguments))),)
        )

    def respond(request):
        index = len(provider.requests)
        if scenario == "short":
            return "NO_REPLY"
        if index == 1 and scenario != "refused":
            return tool(
                "task_control",
                {
                    "action": "accept",
                    "goal": "inspect",
                    "output_kind": "answer",
                    "deliver_artifacts": False,
                },
                "accept",
            )
        if index == (1 if scenario == "refused" else 2):
            return tool("execute_code", {"code": code}, "outer")
        paired = [m for m in request.messages if m.tool_call_id == "outer"]
        assert len(paired) == 1
        outputs.append(json.loads(paired[0].content))
        if scenario in {"normal", "resumed"}:
            return tool("task_control", {"action": "complete"}, "complete")
        return "NO_REPLY"

    provider = FakeLLMProvider(respond)
    settings = make_settings(
        database.url,
        runtime_work_enabled=True,
        enabled_groups_csv="20001",
        code_mode_worker_path=BINARY,
        code_mode_worker_sha256=hashlib.sha256(BINARY.read_bytes()).hexdigest(),
    )
    harness = build_harness(database, settings, provider)
    chat = harness.processor._chat
    chat._tools.social_service = env.service
    chat.runtime.runner.main_contract = MainAgentContract(chat, ShortState(env.store))
    chat.runtime.runner.code_mode_settings = settings
    inbound = InboundMessage(
        message_id="new-user-event",
        event_type="message",
        scope_type=ScopeType.GROUP,
        sender=SenderIdentity("10001"),
        text="Yuki inspect this request quietly",
        bot_user_id="80001",
        group_id="20001",
        mentions_bot=True,
        person_id=env.person,
        space_id=env.space,
        presence_id=env.presence,
        conversation_id=env.context.conversation_id,
        legacy_conversation_key="bot:80001:group:20001",
    )
    reads = []
    execute = chat._tools.execute

    async def record(name, arguments, runtime):
        result = await execute(name, arguments, runtime)
        if name == "get_my_capabilities":
            reads.append((runtime.effective_trigger_event_id, runtime.actor_user_id))
            if scenario == "cancelled":
                await WorkRepository(database).cancel(runtime.effective_conversation_id)
        return result

    chat._tools.execute = record
    sender = MemorySender()
    await harness.processor.handle(inbound, sender)
    async with database.sessions() as reader:
        rows = list(await reader.execute(select(work)))
    if scenario in {"short", "refused"}:
        assert not rows and not reads
        if scenario == "refused":
            assert outputs[0]["error"] == "accept_work_before_execution"
    else:
        assert len(rows) == 1
        row = dict(rows[0]._mapping)
        source = json.loads(row["source_json"])
        assert source["origin"] == "user_message"
        assert source["actor_person_id"] == env.person
        assert source["actor_user_id"] == "10001"
        assert all(
            event_id == source["trigger_event_id"] and actor == "10001" for event_id, actor in reads
        )
        if scenario == "resumed":
            assert row["state"] == "queued" and len(reads) == 32
            resumer = make_work_resumer(
                WorkRepository(database),
                ledger=chat._ledger,
                scopes=chat._conversation_scopes,
                turns=chat._turn_coordinator,
                router=env.router,
                config=chat._runtime_config,
                generate_self=chat.generate_self_initiative,
                generate_wakeup=chat.generate_main_agent_wakeup,
                validate_snapshot=chat.validate_turn_snapshot,
                run_effect=chat.run_effect,
                bindings=chat.runtime.bindings,
            )
            await resumer.resume(row)
            row = await WorkRepository(database).get(row["id"])
        assert row["state"] == ("cancelled" if scenario == "cancelled" else "completed")
        assert len(reads) == count
        assert row["tool_calls"] == count
    assert not sender.messages
    assert not any(action.startswith("send_") for action, _ in env.bot.calls)
