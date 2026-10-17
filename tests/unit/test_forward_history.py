"""Forward references stay observed ledger material and never auto-expand remote content."""

import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

from sqlalchemy import select, update
from tests.support.social_identity_cases import social_env

from qq_ai_bot.conversation.scope import ConversationTurnSnapshot
from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.domain.messages import (
    AttachmentKind,
    InboundMessage,
    MessageAttachment,
    SenderIdentity,
)
from qq_ai_bot.event_prompt import ChatEventPromptRenderer
from qq_ai_bot.persistence.event_repository import EventLedgerRepository
from qq_ai_bot.persistence.models import ChatEventModel
from qq_ai_bot.services.agent_tools import ToolRuntime
from qq_ai_bot.services.attachment_inputs import AttachmentInputService, NativeInput


async def setup_forward(database, tmp_path, segments):
    env = await social_env(database, tmp_path)
    async with database.sessions() as reader:
        event = await reader.scalar(select(ChatEventModel))
        event_id = event.id
    async with database.immediate_session() as writer:
        await writer.execute(
            update(ChatEventModel)
            .where(ChatEventModel.id == event_id)
            .values(segments_json=json.dumps(segments))
        )
    env.bot.calls.clear()
    message = InboundMessage(
        message_id="inbound",
        event_type="message:test",
        scope_type=ScopeType.GROUP,
        sender=SenderIdentity("10001"),
        text="请读取这份转发",
        bot_user_id="80001",
        group_id="20001",
        conversation_id=env.context.conversation_id,
        person_id=env.person,
        presence_id=env.presence,
        space_id=env.space,
        source_event_id=event_id,
    )
    snapshot = ConversationTurnSnapshot(env.context.conversation_id, "forward-test", 1, event_id, 1)
    runtime = ToolRuntime(message, turn_snapshot=snapshot)
    return env, event_id, runtime


async def test_pure_forward_skips_queue_rate_and_media(database, tmp_path):
    _env, event_id, runtime = await setup_forward(
        database, tmp_path, [{"type": "forward", "data": {"id": "outer"}}]
    )
    service = AttachmentInputService.__new__(AttachmentInputService)
    service._pending = 100
    service._pending_limit = 0
    service._limiter = SimpleNamespace(
        allow=AsyncMock(side_effect=AssertionError("no media admission"))
    )
    message = replace(
        runtime.inbound,
        attachments=(MessageAttachment(AttachmentKind.FORWARD, "forward", file="outer"),),
    )
    assert await service.prepare(message, None, None) == NativeInput()
    service._limiter.allow.assert_not_called()
    event = await EventLedgerRepository(database).get_event(event_id)
    for current in [None, event_id]:
        text = ChatEventPromptRenderer.event_content(event, current, "current text")
        assert text.count("[含合併轉發]") == 1


async def test_same_batch_history_reads_original_ledger_per_call(database, tmp_path):
    from tests.support.social_history_cases import history_agent_loop

    env = await social_env(database, tmp_path)
    await history_agent_loop(env, same_batch=True)
