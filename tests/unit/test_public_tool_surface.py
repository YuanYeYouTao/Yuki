"""Chat and automation execute one frozen contract through the same backend."""

import json
import re
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from tests.conftest import build_harness, make_settings

from qq_ai_bot.automation.tools import AutomationToolService
from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.domain.tool_actor import ToolActor
from qq_ai_bot.runtime.origin import TurnOrigin
from qq_ai_bot.services.agent_tools import ToolRuntime
from qq_ai_bot.services.main_agent_backend import MainAgentBackend
from qq_ai_bot.services.main_agent_contract import MainAgentContract
from qq_ai_bot.workspace.short_state import ShortState
from qq_ai_bot.workspace.store import WorkspaceStore


async def setup(database, tmp_path):
    harness = build_harness(database, make_settings(database.url, automation_enabled=True))
    chat = harness.processor._chat
    chat.set_automation_tools(AutomationToolService(SimpleNamespace(enabled=True)))
    contract = MainAgentContract(chat, ShortState(WorkspaceStore(tmp_path / "state")))
    chat._agent_runner.main_contract = contract
    await contract.definitions()
    config = await chat._runtime_config.snapshot()
    actor = ToolActor(
        user_id="10001",
        bot_user_id="7777",
        group_id=None,
        origin=TurnOrigin.SCHEDULED_AUTOMATION,
        instruction="work",
        execution_id="automation:1:execute",
    )
    runtime = ToolRuntime(
        inbound=None,
        gateway=None,
        allow_generic_onebot=False,
        actor_context=actor,
        actor_user_id=actor.user_id,
        origin=actor.origin,
        execution_id=actor.execution_id,
        allow_automation=True,
        allow_work_environment=True,
        scope_type=ScopeType.PRIVATE,
        bot_user_id="7777",
        external_target_id="10001",
        runtime_config=config,
    )
    return chat, contract, runtime


@pytest.mark.asyncio
async def test_one_manifest_without_legacy_aliases(database, tmp_path):
    _, contract, _ = await setup(database, tmp_path)
    tools = await contract.definitions()
    names = [tool.name for tool in tools]
    assert len(names) == len(set(names))
    assert not any(re.search(r"_[0-9a-f]{8}$", name) for name in names)
    assert {
        "automation_create",
        "automation_list_history",
        "send_voice",
        "send_emoji",
        "workspace_write",
    } <= set(names)
    assert not {"send_group_voice", "send_target_emoji", "automation_create_task"} & set(names)
    task_schema = next(t for t in tools if t.name == "automation_create").parameters["properties"][
        "task"
    ]
    assert "capabilities" not in task_schema["properties"]
    tools[0].parameters["tampered"] = True
    assert "tampered" not in (await contract.definitions())[0].parameters


@pytest.mark.asyncio
async def test_scheduled_actor_uses_common_automation_receipt(database, tmp_path):
    chat, _, runtime = await setup(database, tmp_path)
    service = SimpleNamespace(
        enabled=True,
        list_current=AsyncMock(return_value=()),
        timezone=AsyncMock(return_value="Asia/Shanghai"),
    )
    chat.set_automation_tools(AutomationToolService(service))
    receipt = json.loads(await chat._automation_tools.execute("automation_list", "{}", runtime))
    assert receipt["ok"]
    service.list_current.assert_awaited_once_with("10001")
    assert runtime.inbound is None
    with pytest.raises(PermissionError):
        replace(runtime, actor_user_id="9000").require_actor()


@pytest.mark.asyncio
async def test_current_permission_report_without_fake_event(database, tmp_path):
    chat, _, runtime = await setup(database, tmp_path)
    result = json.loads(await chat._tools.execute("get_my_capabilities", "{}", runtime))
    assert result["ok"], result
    assert result["data"]["permission_level"] == "user"
    with pytest.raises(PermissionError):
        chat._tools._capability_report(replace(runtime, actor_is_superuser=True))


@pytest.mark.asyncio
async def test_revocation_precedes_every_common_tool(database, tmp_path):
    chat, _, runtime = await setup(database, tmp_path)
    check = AsyncMock(side_effect=PermissionError("revoked"))
    backend = MainAgentBackend(chat, replace(runtime, before_model_request=check))
    with pytest.raises(PermissionError, match="revoked"):
        await backend.execute("update_short_state", "{}", None)
    check.assert_awaited_once()


@pytest.mark.asyncio
async def test_reply_intents_survive_backend_recreation(database, tmp_path):
    from qq_ai_bot.conversation.delivery import ReplyControlState, ReplySequenceSpec
    from qq_ai_bot.emoji.models import EmojiPlacement, EmojiReplyMode, PendingReplyEffect
    from qq_ai_bot.speech.models import VoiceMode
    from qq_ai_bot.speech.reply_effect import PendingVoiceReplyEffect

    chat, _, runtime = await setup(database, tmp_path)
    effects = [
        PendingVoiceReplyEffect(mode=VoiceMode.VOICE),
        PendingReplyEffect(
            mode=EmojiReplyMode.PREFERRED, placement=EmojiPlacement.AFTER_TEXT, source="agent"
        ),
    ]
    backend = MainAgentBackend(
        chat,
        replace(
            runtime, reply_effects=effects, reply_control=ReplyControlState(ReplySequenceSpec(2))
        ),
    )
    saved = json.loads(json.dumps(backend.export_reply_state()))
    restored_effects = []
    control = ReplyControlState(ReplySequenceSpec(10))
    restored = MainAgentBackend(
        chat, replace(runtime, reply_effects=restored_effects, reply_control=control)
    )
    restored.restore_reply_state(saved)
    assert restored_effects == effects
    assert control.spec.max_messages == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("superuser", [False, True])
async def test_chat_and_scheduled_callable_catalogs_match(database, tmp_path, superuser):
    from tests.unit.test_automation_runtime import _inbound

    from qq_ai_bot.conversation.delivery import ReplyControlState, ReplySequenceSpec

    chat, _, scheduled = await setup(database, tmp_path)
    scheduled = replace(
        scheduled,
        actor_is_superuser=superuser,
        allow_admin_actions=superuser,
        allow_generic_onebot=superuser,
        reply_effects=[],
        reply_control=ReplyControlState(ReplySequenceSpec(10)),
    )
    normal = replace(
        scheduled, inbound=_inbound(), actor_context=None, origin=TurnOrigin.USER_MESSAGE
    )
    catalogs = []
    for runtime in (normal, scheduled):
        backend = MainAgentBackend(chat, runtime)
        await backend.prepare()
        catalogs.append(
            {
                entry.descriptor.model_name: entry.descriptor.as_chat_tool()
                for entry in backend._catalog.entries
            }
        )
    assert catalogs[0] == catalogs[1]


@pytest.mark.asyncio
async def test_scheduled_social_uses_actor_without_qq_message(database, tmp_path):
    from qq_ai_bot.capabilities.invocation import ToolInvocationContext, current_invocation
    from qq_ai_bot.identity.canonical_repository import active_person_id_for
    from qq_ai_bot.social.agent_adapter import invoke_social

    _, _, runtime = await setup(database, tmp_path)
    async with database.sessions() as session:
        person = await active_person_id_for(session, "10001")
    runtime = replace(
        runtime,
        conversation_id="conversation",
        actor_context=replace(runtime.actor_context, conversation_id="conversation"),
    )
    service = SimpleNamespace(
        database=database, execute=AsyncMock(return_value={"status": "succeeded"})
    )
    token = current_invocation.set(
        ToolInvocationContext(runtime=runtime, call_id="send-1", execution_id=runtime.execution_id)
    )
    try:
        await invoke_social(
            service,
            "send_private_message",
            {"subject_ref": "current_speaker", "text": "hi"},
            runtime,
        )
    finally:
        current_invocation.reset(token)
    context = service.execute.await_args.args[2]
    assert context.person_refs["current_speaker"] == person
    assert context.reply_message_id is None
    assert runtime.execution_id in context.turn_id


@pytest.mark.asyncio
async def test_scheduled_reply_prepares_voice_with_common_service(database, tmp_path):
    from datetime import UTC, datetime

    from qq_ai_bot.automation.authority import AuthorityContext
    from qq_ai_bot.automation.delivery import deliver_reply
    from qq_ai_bot.automation.models import AutomationContext
    from qq_ai_bot.automation.registry import CapabilityExecutionContext
    from qq_ai_bot.domain.messages import AttachmentKind, OutboundMedia, OutboundMessage

    chat, _, runtime = await setup(database, tmp_path)
    prepared = SimpleNamespace(
        message=OutboundMessage(
            media=(
                OutboundMedia(kind=AttachmentKind.AUDIO, local_path="/voice.wav", generation_id=2),
            )
        ),
        suppress_text=True,
    )
    chat._speech_effects = SimpleNamespace(
        prepare=AsyncMock(return_value=prepared), record_success=AsyncMock()
    )
    gateway = SimpleNamespace(
        send_private=AsyncMock(), send_voice=AsyncMock(return_value={"message_id": "9"})
    )
    now = datetime.now(UTC)
    context = CapabilityExecutionContext(
        authority=AuthorityContext(
            origin=runtime.origin,
            actor_user_id="10001",
            actor_is_superuser=False,
            bot_user_id="7777",
        ),
        automation_id=1,
        automation_run_id=2,
        step_id="deliver",
        creator_user_id="10001",
        bot_user_id="7777",
        current_group_id=None,
        scheduled_for=now,
        actual_started_at=now,
        local_time=now,
        timezone="Asia/Shanghai",
        automation_context=AutomationContext(),
        conversation_key="private",
        revalidate_authority=AsyncMock(),
    )
    count = await deliver_reply(
        {
            "text": "完成了",
            "user_id": "10001",
            "reply_state": {"effects": [{"kind": "voice", "mode": "voice"}]},
        },
        context,
        gateway,
        chat=chat,
    )
    assert count == 1
    gateway.send_voice.assert_awaited_once()
    gateway.send_private.assert_not_awaited()
    assert (
        chat._speech_effects.prepare.await_args.kwargs["actor"].origin
        is TurnOrigin.SCHEDULED_AUTOMATION
    )
    chat._speech_effects.record_success.assert_awaited_once()
