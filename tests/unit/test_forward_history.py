"""Forward references stay observed ledger material and never auto-expand remote content."""

import json

from sqlalchemy import select, update
from tests.support.social_identity_cases import social_env

from yuki.conversation.scope import ConversationTurnSnapshot
from yuki.domain.conversations import ScopeType
from yuki.domain.messages import (
    InboundMessage,
    SenderIdentity,
)
from yuki.event_prompt import ChatEventPromptRenderer
from yuki.persistence.event_repository import EventLedgerRepository
from yuki.persistence.models import ChatEventModel
from yuki.services.agent_tools import ToolRuntime


async def setup_forward(database, tmp_path, segments):
    env = await social_env(database, tmp_path)
    async with database.read_session() as reader:
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


async def test_pure_forward_remains_an_unexpanded_ledger_marker(database, tmp_path):
    _env, event_id, _runtime = await setup_forward(
        database, tmp_path, [{"type": "forward", "data": {"id": "outer"}}]
    )
    event = await EventLedgerRepository(database).get_event(event_id)
    for current in [None, event_id]:
        text = ChatEventPromptRenderer.event_content(event, current, "current text")
        assert text.count("[含合併轉發]") == 1


async def test_same_batch_history_reads_original_ledger_per_call(database, tmp_path):
    from tests.support.social_history_cases import history_agent_loop

    env = await social_env(database, tmp_path)
    await history_agent_loop(env, same_batch=True)
