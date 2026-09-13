"""Reply telemetry resolves trusted ledger identity before taking the write lock."""

import pytest
from sqlalchemy import event, select
from tests.support.social_identity_cases import social_env

from qq_ai_bot.conversation.cadence import ReplyEffectRepository
from qq_ai_bot.conversation.db_models import ReplyEffectEventModel
from qq_ai_bot.persistence.models import ChatEventModel


@pytest.mark.asyncio
async def test_reply_effect_correlation_uses_primary_key_before_insert(database, tmp_path):
    env = await social_env(database, tmp_path)
    async with database.sessions() as session:
        trigger = await session.scalar(
            select(ChatEventModel).where(
                ChatEventModel.canonical_conversation_id == env.context.conversation_id
            )
        )
    statements = []

    def before(_conn, _cursor, statement, _params, _context, _many):
        statements.append(statement)

    event.listen(database.engine.sync_engine, "before_cursor_execute", before)
    repository = ReplyEffectRepository(database)
    try:
        args = dict(
            conversation_key="reply-cadence-test",
            source_event_id="opaque-delivery-key",
            trigger_event_id=trigger.id,
            canonical_conversation_id=trigger.canonical_conversation_id,
            bot_user_id=trigger.bot_user_id,
            ingress_presence_id=trigger.ingress_presence_id,
            text_sent=True,
            voice_sent=False,
            emoji_sent=False,
            voice_request_basis="none",
        )
        await repository.record(**args)
        await repository.record(**args)
    finally:
        event.remove(database.engine.sync_engine, "before_cursor_execute", before)
    inserts = [
        i for i, sql in enumerate(statements) if sql.startswith("INSERT INTO reply_effect_events")
    ]
    lookups = [i for i, sql in enumerate(statements) if "FROM chat_events" in sql]
    assert len(inserts) == 1 and lookups and max(lookups) < inserts[0]
    assert all("WHERE chat_events.id =" in statements[i] for i in lookups)
    async with database.sessions() as session:
        rows = list(
            await session.scalars(
                select(ReplyEffectEventModel).where(
                    ReplyEffectEventModel.canonical_conversation_id
                    == trigger.canonical_conversation_id
                )
            )
        )
        assert len(rows) == 1
