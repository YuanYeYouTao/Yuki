"""Internal provenance remains stable when transport credentials collide/change."""

from dataclasses import replace

import pytest
from sqlalchemy import select
from tests.support.social_identity_cases import social_env

from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel
from qq_ai_bot.domain.messages import InboundMessage, SenderIdentity
from qq_ai_bot.event_prompt import ChatEventPromptRenderer
from qq_ai_bot.persistence.event_repository import EventLedgerRepository
from qq_ai_bot.persistence.models import ChatEventModel
from qq_ai_bot.persistence.web_repository import WebSearchSourceRepository
from qq_ai_bot.sandbox.source_recovery import recover_source
from qq_ai_bot.sandbox.task_repository import SandboxTaskRepository
from qq_ai_bot.web.base import WebSearchError
from qq_ai_bot.web.models import WebSearchResponse, WebSearchSource


@pytest.mark.asyncio
async def test_recovery_requires_internal_anchor_and_ignores_qq_credential(database, tmp_path):
    env = await social_env(database, tmp_path)
    async with database.sessions() as session:
        row = await session.scalar(select(ChatEventModel))
        conv = await session.get(CanonicalConversationModel, row.canonical_conversation_id)
        source = dict(
            origin="user_message",
            generation=conv.generation,
            trigger_event_id=row.id,
            trigger_id="wrong-qq-id",
            bot_user_id=row.bot_user_id,
            actor_user_id=row.sender_user_id,
            presence_id=row.ingress_presence_id,
        )
    recovered = await recover_source(
        database, env.context.conversation_id, source, request_id="run"
    )
    assert recovered.event_id == row.id
    with pytest.raises(ValueError, match="invalid_task_event_anchor"):
        await SandboxTaskRepository(database).prepare(
            "unanchored",
            {"command": "must not run"},
            {**source, "conversation_id": env.context.conversation_id, "trigger_event_id": None},
        )
    for invalid in (None, True, "1", 0):
        with pytest.raises(ValueError, match="invalid_task_event_anchor"):
            await recover_source(
                database,
                env.context.conversation_id,
                {**source, "trigger_event_id": invalid, "trigger_id": "inbound"},
                request_id="run",
            )


@pytest.mark.asyncio
async def test_web_provenance_uses_event_and_execution_identity(database, tmp_path):
    env = await social_env(database, tmp_path)
    ledger = EventLedgerRepository(database)
    async with database.sessions() as session:
        event_id = await session.scalar(select(ChatEventModel.id))
    original = await ledger.get_event(event_id)
    repository = WebSearchSourceRepository(database)
    response = WebSearchResponse(
        query="q",
        sources=(
            WebSearchSource(
                source_id="s",
                title="Evidence",
                url="https://example.com/evidence",
                domain="example.com",
                snippet="actual evidence",
                relevant_content="actual evidence",
            ),
        ),
        latency_seconds=0,
        provider_request_id=None,
    )
    await repository.save_response(
        conversation_key="conversation",
        trigger_message_id="reused",
        trigger_event_id=event_id,
        provider="test",
        response=response,
        max_runs=10,
        canonical_conversation_id=env.context.conversation_id,
    )
    assert await repository.used_url_for_trigger(
        conversation_key="conversation",
        trigger_event_id=event_id,
        url="https://example.com/evidence",
    )
    assert not await repository.for_trigger(
        conversation_key="conversation", trigger_event_id=event_id + 1
    )
    assert not await repository.for_trigger(conversation_key="other", trigger_event_id=event_id)
    with pytest.raises(WebSearchError, match="内部"):
        await repository.for_trigger(conversation_key="conversation", trigger_event_id=None)
    await repository.save_response(
        conversation_key="conversation",
        trigger_message_id="reused",
        execution_id="automation:run:step",
        provider="test",
        response=response,
        max_runs=10,
        canonical_conversation_id=env.context.conversation_id,
    )
    assert (
        len(
            await repository.for_trigger(
                conversation_key="conversation",
                trigger_event_id=None,
                execution_id="automation:run:step",
            )
        )
        == 1
    )
    assert not await repository.for_trigger(
        conversation_key="conversation", trigger_event_id=None, execution_id="another-run"
    )
    # The same QQ id in a different event must not substitute current user content.
    other = replace(original, id=event_id + 1)
    assert ChatEventPromptRenderer.event_content(other, event_id, "replacement") == original.content
    assert ChatEventPromptRenderer.event_content(original, event_id, "replacement") == "replacement"
    inbound = InboundMessage(
        message_id="reused",
        event_type="message",
        scope_type=original.scope_type,
        sender=SenderIdentity(original.sender_user_id),
        text="hello",
        source_event_id=event_id,
    )
    assert inbound.source_key == replace(inbound, message_id="changed").source_key
    assert inbound.source_key != replace(inbound, source_event_id=event_id + 1).source_key
    with pytest.raises(ValueError, match="missing_internal"):
        _ = replace(inbound, source_event_id=None).source_key
