"""Offline intents wait; ended original entries and missing media cannot become new effects."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from tests.support.background_authority import approve_background_plugin
from tests.support.memory_identity import transfer_account
from tests.support.social_identity_cases import social_env

from yuki.conversation.hydrate import ensure_canonical_conversation
from yuki.identity.canonical_repository import find_identity_binding
from yuki.identity.db_models import CanonicalPersonModel
from yuki.persistence.event_repository import EventLedgerRepository
from yuki.plugin_host.db_models import PluginNotificationOutboxModel
from yuki.plugin_host.media_artifacts import PluginMediaArtifactStore
from yuki.plugin_host.notification_delivery import (
    PlatformNotificationTransport,
    PluginNotificationOutboxWorker,
)
from yuki.services.effect_gate import ConversationEffectGate
from yuki.workspace.store import WorkspaceStore
from yuki_plugin_sdk.models import NotificationTarget, PublishNotificationRequest


async def prepare_fixture(database, tmp_path, *, private=False):
    env = await social_env(database, tmp_path)
    repository = await approve_background_plugin(
        database,
        plugin_id="prepare-source",
        bot_user_id="80001",
        group_id="20001",
        creator_user_id="10001",
    )
    conversation_id = env.context.conversation_id
    if private:
        async with database.immediate_session() as session:
            binding = await find_identity_binding(session, "10001")
            entry = await ensure_canonical_conversation(
                session,
                kind="private",
                person_id=env.person,
                identity_binding_id=binding.id,
                presence_id=env.presence,
                primary_scope_key="bot:80001:private:10001",
            )
        conversation_id = entry.conversation_id
        await repository.grant_target(
            plugin_id="prepare-source",
            target=NotificationTarget(
                target_type="private",
                target_id=env.person,
                conversation_id=conversation_id,
            ),
            bot_user_id="80001",
            created_by_user_id=env.person,
            presence_id=env.presence,
        )
    publication = await repository.publish(
        plugin_id="prepare-source",
        request=PublishNotificationRequest(
            target=NotificationTarget(
                target_type="private" if private else "group",
                target_id=env.person if private else env.space,
                conversation_id=conversation_id,
            ),
            external_source="fixture",
            event_key="original",
            event_type="fixture",
            summary="original publication",
            text="original text",
            occurred_at=datetime.now(UTC),
        ),
    )
    worker = PluginNotificationOutboxWorker(
        workspace_store=WorkspaceStore(tmp_path / "workspace"),
        repository=repository,
        artifacts=PluginMediaArtifactStore(database, tmp_path / "media"),
        ledger=EventLedgerRepository(database),
        effect_gate=ConversationEffectGate(),
        transport=PlatformNotificationTransport(database=database, router=env.router),
    )
    return env, repository, worker, publication.source_event_id


@pytest.mark.asyncio
async def test_offline_original_notification_stays_pending_without_a_physical_claim(
    database, tmp_path
):
    env, repository, worker, event_id = await prepare_fixture(database, tmp_path)
    env.registry.disconnect(env.bot)
    await worker._prepare_pending()
    async with database.read_session() as session:
        row = await session.scalar(
            select(PluginNotificationOutboxModel).where(
                PluginNotificationOutboxModel.source_event_id == event_id,
            )
        )
        assert row.status == "pending" and row.attempts == 0 and row.prepared_json is None
    assert await repository.claim_outbox() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("cause", ["ended_private_entry", "revoked_source", "missing_media"])
async def test_unprepared_notification_closes_only_its_real_unusable_source(
    database, tmp_path, cause
):
    env, repository, worker, event_id = await prepare_fixture(
        database,
        tmp_path,
        private=cause == "ended_private_entry",
    )
    if cause == "ended_private_entry":
        await transfer_account(database, "10001", "10002")
    else:
        async with database.immediate_session() as session:
            if cause == "revoked_source":
                person = await session.get(CanonicalPersonModel, env.person)
                person.enabled = False
            else:
                row = await session.scalar(
                    select(PluginNotificationOutboxModel).where(
                        PluginNotificationOutboxModel.source_event_id == event_id,
                    )
                )
                row.part_type, row.media_handle_id, row.text = "media", None, ""
    env.registry.disconnect(env.bot)
    env.bot.calls.clear()
    await worker._prepare_pending()
    async with database.read_session() as session:
        row = await session.scalar(
            select(PluginNotificationOutboxModel).where(
                PluginNotificationOutboxModel.source_event_id == event_id,
            )
        )
        assert row.status == "failed" and row.attempts == 0 and row.prepared_json is None
        assert (
            row.last_error_category
            == {
                "ended_private_entry": "canonical_owner_mismatch",
                "revoked_source": "canonical_owner_disabled",
                "missing_media": "media_handle_missing",
            }[cause]
        )
    assert await repository.claim_outbox() is None
    assert env.bot.calls == []
