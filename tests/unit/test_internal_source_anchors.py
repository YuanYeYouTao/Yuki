"""Internal provenance remains stable when transport credentials collide/change."""

from dataclasses import replace

import pytest
from sqlalchemy import select
from tests.support.social_identity_cases import social_env

from yuki.conversation.canonical_db_models import CanonicalConversationModel
from yuki.domain.messages import InboundMessage, SenderIdentity
from yuki.event_prompt import ChatEventPromptRenderer
from yuki.persistence.event_repository import EventLedgerRepository
from yuki.persistence.models import ChatEventModel
from yuki.sandbox.task_repository import SandboxTaskRepository
from yuki.services.execution_sources import recover_source


@pytest.mark.asyncio
async def test_recovery_requires_internal_anchor_and_ignores_qq_credential(database, tmp_path):
    env = await social_env(database, tmp_path)
    async with database.read_session() as session:
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
    for invalid in (None, True, "1", 0):
        with pytest.raises(ValueError, match="invalid_task_event_anchor"):
            await SandboxTaskRepository(database).prepare(
                "unanchored",
                {"command": "must not run"},
                {
                    **source,
                    "conversation_id": env.context.conversation_id,
                    "trigger_event_id": invalid,
                },
            )
        with pytest.raises(ValueError, match="invalid_task_event_anchor"):
            await recover_source(
                database,
                env.context.conversation_id,
                {**source, "trigger_event_id": invalid, "trigger_id": "inbound"},
                request_id="run",
            )


@pytest.mark.asyncio
async def test_sandbox_self_source_keeps_original_execution_without_message_or_work(database):
    import json

    from tests.unit.test_self_initiative_runtime import self_source

    from yuki.services.execution_sources import recover_execution_source

    source, _, _ = await self_source(database)
    original_execution = f"initiative:{source['initiative_run_id']}"
    recovered = await recover_execution_source(
        database, source["conversation_id"], source, request_id=original_execution
    )
    actor = recovered.actor(original_execution)
    source = {**source, "parent_execution_id": actor.execution_id}
    saved = await SandboxTaskRepository(database).prepare(
        "self-command", {"command": "render"}, source
    )
    assert json.loads(saved.source_json) == source
    assert actor.principal_kind == "self" and actor.user_id == "" and actor.event_id is None


@pytest.mark.asyncio
async def test_prompt_and_inbound_identity_use_internal_event_not_platform_id(database, tmp_path):
    await social_env(database, tmp_path)
    ledger = EventLedgerRepository(database)
    async with database.read_session() as session:
        event_id = await session.scalar(select(ChatEventModel.id))
    original = await ledger.get_event(event_id)
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
