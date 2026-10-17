"""Generic freshness and CAS gates for plugin-triggered main-conversation turns."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from yuki.conversation.canonical_db_models import (
    CanonicalConversationModel,
)
from yuki.domain.conversations import ConversationScope, ScopeType
from yuki.persistence.database import Database
from yuki.persistence.event_repository import EventLedgerRepository
from yuki.persistence.models import ChatEventModel
from yuki.plugin_host.db_models import (
    PluginBackgroundTurnJobModel,
    PluginInstallationModel,
    PluginNotificationOutboxModel,
)
from yuki.plugin_host.notification_repository import (
    TURN_ERROR_LATER_HUMAN,
    TURN_ERROR_SUPERSEDED_COVERED,
    PluginNotificationRepository,
)
from yuki_plugin_sdk.api import PLUGIN_API_VERSION
from yuki_plugin_sdk.models import NotificationTarget, PublishNotificationRequest

_NOW = datetime(2026, 8, 26, 8, tzinfo=UTC)
_PLUGIN_ID = "test.external-fence"


async def _seed_job(
    database: Database,
) -> tuple[PluginNotificationRepository, int, str]:
    async with database.immediate_session() as session:
        session.add(
            PluginInstallationModel(
                plugin_id=_PLUGIN_ID,
                name="External fence fixture",
                version="1.0.0",
                plugin_api=PLUGIN_API_VERSION,
                yuki_requires=">=3.8",
                manifest_hash="fixture",
                entrypoint="fixture:plugin",
                status="running",
                enabled=True,
                approved_permissions_json="[]",
                requested_permissions_json="[]",
                failure_count=0,
                last_error_category=None,
                discovered_at=_NOW,
                approved_at=_NOW,
                started_at=_NOW,
                updated_at=_NOW,
            )
        )
    repository = PluginNotificationRepository(database)
    source, _created = await EventLedgerRepository(database).append(
        bot_user_id="8000",
        platform_message_id="human-before-external",
        scope_type=ScopeType.PRIVATE,
        sender_user_id="1001",
        private_peer_user_id="1001",
        direction="inbound",
        content="before external",
    )
    target = NotificationTarget(
        target_type="private",
        target_id=source.author_person_id,
        conversation_id=source.canonical_conversation_id,
    )
    await repository.grant_target(
        plugin_id=_PLUGIN_ID,
        target=target,
        bot_user_id="8000",
        created_by_user_id=source.author_person_id,
        presence_id=source.ingress_presence_id,
    )
    receipt = await repository.publish(
        plugin_id=_PLUGIN_ID,
        request=PublishNotificationRequest(
            event_key="fixture-1",
            event_type="fixture",
            external_source="fixture",
            target=target,
            occurred_at=_NOW,
            summary="external summary",
            ask_agent=True,
            agent_intent="react briefly",
        ),
    )
    async with database.sessions() as session:
        event = await session.get(ChatEventModel, receipt.source_event_id)
        assert event is not None and event.canonical_conversation_id
        conversation_id = event.canonical_conversation_id
    return repository, receipt.source_event_id, conversation_id


@pytest.mark.asyncio
async def test_reclaimed_turn_attempt_rejects_old_worker_mutations(database: Database) -> None:
    repository, _source_id, _conversation_id = await _seed_job(database)
    first = await repository.claim_turn(lease_seconds=1)
    assert first is not None
    async with database.immediate_session() as session:
        row = await session.get(PluginBackgroundTurnJobModel, first.id)
        assert row is not None
        row.lease_until = _NOW - timedelta(seconds=1)
    second = await repository.claim_turn()
    assert second is not None and second.attempts == first.attempts + 1
    assert not await repository.fail_turn(
        first.id,
        attempt=first.attempts,
        error_category="stale-worker",
    )
    async with database.sessions() as session:
        row = await session.get(PluginBackgroundTurnJobModel, first.id)
        assert row is not None
        assert row.status == "processing"
        assert row.attempts == second.attempts


@pytest.mark.asyncio
async def test_reset_source_is_terminal_before_claim(database: Database) -> None:
    repository, source_id, conversation_id = await _seed_job(database)
    async with database.immediate_session() as session:
        conversation = await session.get(CanonicalConversationModel, conversation_id)
        assert conversation is not None
        conversation.generation += 1
        conversation.starts_after_event_id = source_id
        conversation.last_generation_change_event_id = source_id
        conversation.revision += 1
        conversation.updated_at = _NOW
    assert await repository.claim_turn() is None
    async with database.sessions() as session:
        row = await session.scalar(select(PluginBackgroundTurnJobModel))
        assert row is not None
        assert row.status == "cancelled"
        assert row.last_error_category == TURN_ERROR_SUPERSEDED_COVERED


@pytest.mark.asyncio
async def test_later_human_message_is_terminal_before_claim(database: Database) -> None:
    repository, _source_id, _conversation_id = await _seed_job(database)
    await EventLedgerRepository(database).append(
        bot_user_id="8000",
        platform_message_id="human-after-external",
        scope_type=ScopeType.PRIVATE,
        sender_user_id="1001",
        private_peer_user_id="1001",
        direction="inbound",
        content="human message",
    )
    assert await repository.claim_turn() is None
    async with database.sessions() as session:
        row = await session.scalar(select(PluginBackgroundTurnJobModel))
        assert row is not None
        assert row.status == "cancelled"
        assert row.last_error_category == TURN_ERROR_LATER_HUMAN


@pytest.mark.asyncio
async def test_message_history_filters_external_before_limit(database: Database) -> None:
    _repository, _source_id, _conversation_id = await _seed_job(database)
    ledger = EventLedgerRepository(database)
    await ledger.append(
        bot_user_id="8000",
        platform_message_id="human-after-external",
        scope_type=ScopeType.PRIVATE,
        sender_user_id="1001",
        private_peer_user_id="1001",
        direction="inbound",
        content="after external",
    )
    rows = await ledger.list_scope_recent(
        ConversationScope.private("8000", "1001"),
        limit=2,
        message_only=True,
    )
    assert [row.content for row in rows] == ["before external", "after external"]
    searched = await ledger.search(
        keyword="external",
        user_id="1001",
        limit=10,
        message_only=True,
    )
    assert [row.content for row in searched] == ["before external", "after external"]
    with pytest.raises(ValueError, match="require a conversation, owner, or time bound"):
        await ledger.search(keyword="x", limit=10, message_only=True)


@pytest.mark.asyncio
async def test_finish_rechecks_generation_without_enqueuing_agent_reply(database: Database) -> None:
    repository, _source_id, conversation_id = await _seed_job(database)
    job = await repository.claim_turn()
    assert job is not None
    async with database.immediate_session() as session:
        owner = await session.get(CanonicalConversationModel, conversation_id)
        owner.generation += 1
    assert not await repository.finish_turn(
        job.id,
        attempt=job.attempts,
        generation=job.generation,
        tool_calls_used=0,
        model_requests=1,
    )
    async with database.sessions() as session:
        stored = await session.get(PluginBackgroundTurnJobModel, job.id)
        assert stored is not None
        assert stored.status == "cancelled"
        assert stored.last_error_category == TURN_ERROR_SUPERSEDED_COVERED
        reply = await session.scalar(
            select(PluginNotificationOutboxModel).where(
                PluginNotificationOutboxModel.part_key == "agent_reply"
            )
        )
        assert reply is None
