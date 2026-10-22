"""An erased ledger row does not erase or repoint its original delivery receipt."""

from dataclasses import replace

from sqlalchemy import delete
from tests.support.social_identity_cases import social_env

from yuki.persistence.models import ChatEventModel
from yuki.social.db_models import SocialOperationModel


async def test_missing_original_keeps_receipt_and_cannot_read_next_send(database, tmp_path):
    env = await social_env(database, tmp_path)
    sent = await env.service.execute("send", {"text": "original"}, env.context)
    event_id = sent["event_id"]
    assert sent["original_event_id"] == event_id
    assert sent["original_conversation_id"] == env.context.conversation_id
    async with database.immediate_session() as writer:
        await writer.execute(delete(ChatEventModel).where(ChatEventModel.id == event_id))
    current = await env.service.receipts.get(sent["operation_id"])
    assert current.event_id is None
    assert current.original_event_id == event_id
    assert current.original_conversation_id == env.context.conversation_id
    next_send = await env.service.execute(
        "send", {"text": "new content"}, replace(env.context, call_id="next-send")
    )
    assert next_send["event_id"] > event_id
    calls = list(env.bot.calls)
    recovered = await env.service.execute("send", {"text": "original"}, env.context)
    assert recovered["status"] == "succeeded"
    assert recovered["original_event_id"] == event_id
    assert recovered["event_id"] is None
    assert recovered["delivered_text_available"] is False
    assert "delivered_text" not in recovered
    assert env.bot.calls == calls
    async with database.sessions() as reader:
        row = await reader.get(SocialOperationModel, sent["operation_id"])
        assert row.original_event_id == event_id
        assert row.original_conversation_id == env.context.conversation_id
