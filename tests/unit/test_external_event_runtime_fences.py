"""Generic freshness and CAS gates for plugin-triggered main-conversation turns."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from qq_ai_bot.conversation.canonical_db_models import (
    CanonicalConversationModel,
    CanonicalConversationRollupEmergencyOverlayModel,
    CanonicalConversationRollupModel,
)
from qq_ai_bot.conversation.rollup.coverage import session_effective_coverage
from qq_ai_bot.domain.conversations import ConversationScope, ScopeType
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.event_repository import EventLedgerRepository
from qq_ai_bot.persistence.models import ChatEventModel
from qq_ai_bot.plugin_host.db_models import (
    PluginBackgroundTurnJobModel,
    PluginInstallationModel,
    PluginNotificationOutboxModel,
)
from qq_ai_bot.plugin_host.notification_repository import (
    TURN_ERROR_LATER_HUMAN,
    TURN_ERROR_SUPERSEDED_COVERED,
    PluginNotificationRepository,
)
from yuki_plugin_sdk.api import PLUGIN_API_VERSION
from yuki_plugin_sdk.models import NotificationTarget, PublishNotificationRequest

_NOW = datetime(2026, 8, 26, 8, tzinfo=UTC)
_PLUGIN_ID = "test.external-fence"


@dataclass(frozen=True)
class _Semantic:
    generation: int
    revision: int
    covered_through_event_id: int


@dataclass(frozen=True)
class _Overlay:
    generation: int
    base_semantic_revision: int
    covered_through_event_id: int
    summary_text: str


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
    target = NotificationTarget(target_type="private", target_id="1001")
    await repository.grant_target(
        plugin_id=_PLUGIN_ID,
        target=target,
        bot_user_id="8000",
        created_by_user_id="1001",
    )
    await EventLedgerRepository(database).append(
        bot_user_id="8000",
        platform_message_id="human-before-external",
        scope_type=ScopeType.PRIVATE,
        sender_user_id="1001",
        private_peer_user_id="1001",
        direction="inbound",
        content="before external",
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


def test_effective_coverage_prefers_valid_overlay_even_if_semantic_watermark_is_invalid() -> None:
    semantic = _Semantic(generation=1, revision=7, covered_through_event_id=999)
    overlay = _Overlay(
        generation=1,
        base_semantic_revision=7,
        covered_through_event_id=8,
        summary_text="overlay",
    )
    assert (
        session_effective_coverage(
            generation=1,
            starts_after=3,
            last_event_id=10,
            overlay=overlay,
            semantic=semantic,
        )
        == 8
    )


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
        conversation.covered_through_event_id = source_id
        conversation.revision += 1
        conversation.updated_at = _NOW
    assert await repository.claim_turn() is None
    async with database.sessions() as session:
        row = await session.scalar(select(PluginBackgroundTurnJobModel))
        assert row is not None
        assert row.status == "cancelled"
        assert row.last_error_category == TURN_ERROR_SUPERSEDED_COVERED


@pytest.mark.asyncio
async def test_valid_overlay_coverage_is_terminal_before_claim(database: Database) -> None:
    repository, source_id, conversation_id = await _seed_job(database)
    async with database.immediate_session() as session:
        session.add(
            CanonicalConversationRollupModel(
                conversation_id=conversation_id,
                generation=1,
                covered_through_event_id=0,
                summary_text="semantic",
                summary_kind="model",
                source_fingerprint="a" * 64,
                revision=3,
                created_at=_NOW,
                updated_at=_NOW,
            )
        )
        session.add(
            CanonicalConversationRollupEmergencyOverlayModel(
                conversation_id=conversation_id,
                generation=1,
                covered_through_event_id=source_id,
                summary_text="overlay",
                source_fingerprint="b" * 64,
                base_semantic_revision=3,
                revision=1,
                created_at=_NOW,
                updated_at=_NOW,
            )
        )
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
    with pytest.raises(ValueError, match="require a QQ"):
        await ledger.search(keyword="x", limit=10, message_only=True)


@pytest.mark.asyncio
async def test_finish_rechecks_coverage_before_enqueuing_agent_reply(database: Database) -> None:
    repository, source_id, conversation_id = await _seed_job(database)
    job = await repository.claim_turn()
    assert job is not None
    async with database.immediate_session() as session:
        session.add(
            CanonicalConversationRollupModel(
                conversation_id=conversation_id,
                generation=job.generation,
                covered_through_event_id=source_id,
                summary_text="semantic",
                summary_kind="model",
                source_fingerprint="c" * 64,
                revision=1,
                created_at=_NOW,
                updated_at=_NOW,
            )
        )
    assert not await repository.finish_turn(
        job.id,
        attempt=job.attempts,
        generation=job.generation,
        text="must not escape",
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
