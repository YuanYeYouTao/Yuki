"""A local notification is one durable ledger effect, including its media original."""

import asyncio
import io
import json
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from PIL import Image
from sqlalchemy import func, select
from tests.integration.test_notification_platform_entries import install
from tests.unit.test_app_messages import app_env as app_env
from tests.unit.test_client_api import bearer
from tests.unit.test_client_works import owner
from tests.unit.test_notification_preparation_sources import prepare_fixture
from tests.unit.test_personal_entry_contract import admin as admin

from yuki.conversation.canonical_db_models import CanonicalConversationModel
from yuki.gateway.providers.app.social import AppSocialOperations
from yuki.identity.db_models import IdentityBindingModel, PresenceModel
from yuki.persistence.models import ChatEventArtifactRefModel, ChatEventModel
from yuki.plugin_host.db_models import PluginNotificationOutboxModel
from yuki.plugin_host.media_artifacts import PluginMediaArtifactStore
from yuki.plugin_host.notification_delivery import (
    PlatformNotificationTransport,
    PluginNotificationOutboxWorker,
)
from yuki.services.effect_gate import ConversationEffectGate
from yuki_plugin_sdk.models import NotificationTarget, PublishNotificationRequest


async def prepared(env, database, tmp_path, *, media=False):
    device, person = await owner(env, database)
    principal = await env.identity.authenticate(device["token"])
    async with database.read_session() as reader:
        conversation = await reader.get(CanonicalConversationModel, principal.conversation_id)
        presence = conversation.entry_presence_id
    repository = await install(database)
    target = NotificationTarget(
        target_type="private", target_id=person, conversation_id=principal.conversation_id
    )
    await repository.grant_target(
        plugin_id="native-notification",
        target=target,
        bot_user_id="yuki-app",
        created_by_user_id=person,
        presence_id=presence,
    )
    artifacts = PluginMediaArtifactStore(database, tmp_path / "plugin-media")
    content, handles = b"", ()
    if media:
        stream = io.BytesIO()
        Image.new("RGB", (2, 2), "red").save(stream, "PNG")
        content = stream.getvalue()
        handle = await artifacts.create(
            plugin_id="native-notification",
            data=content,
            content_type="image/png",
            filename="red.png",
            ttl_seconds=60,
            storage_mb=5,
        )
        handles = (handle.handle_id,)
    await repository.publish(
        plugin_id="native-notification",
        request=PublishNotificationRequest(
            event_key="original",
            event_type="fixture",
            external_source="fixture",
            target=target,
            occurred_at=datetime.now(UTC),
            summary="notification",
            text="" if media else "hello",
            media_handles=handles,
        ),
    )
    worker = PluginNotificationOutboxWorker(
        repository=repository,
        artifacts=artifacts,
        ledger=env.harness.ledger,
        workspace_store=env.store,
        transport=PlatformNotificationTransport(database=database, router=env.social.router),
        effect_gate=ConversationEffectGate(),
    )
    await worker._prepare_pending()
    item = await repository.claim_outbox()
    assert item is not None
    assert item.prepared["route"]["local_ledger"] is True
    assert item.prepared["route"]["provider_id"] == "app"
    return worker, repository, item, device, principal, content


@pytest.mark.parametrize("media", [False, True])
async def test_local_notification_is_visible_in_client_history_and_files(
    app_env, database, tmp_path, monkeypatch, media
):
    env = app_env
    worker, repository, item, device, principal, content = await prepared(
        env, database, tmp_path, media=media
    )
    native = AsyncMock(side_effect=AssertionError("local delivery invoked a native sender"))
    monkeypatch.setattr(AppSocialOperations, "send", native)
    await worker._deliver(item)
    await worker._deliver(item)
    assert await repository.claim_outbox() is None
    native.assert_not_called()
    async with database.read_session() as reader:
        row = await reader.get(PluginNotificationOutboxModel, item.id)
        assert row.status == "sent" and row.platform_message_id is None
        event = await reader.get(ChatEventModel, row.event_id)
        assert event.origin_key is None and event.native_message_id is None
        assert event.canonical_conversation_id == principal.conversation_id
        event_id = event.id
        refs = list(
            await reader.scalars(
                select(ChatEventArtifactRefModel).where(
                    ChatEventArtifactRefModel.event_id == event_id
                )
            )
        )
    headers = bearer(device["token"])
    history = await env.client.get(
        f"/client/v1/conversations/{principal.conversation_id}/events", headers=headers
    )
    assert history.status_code == 200, history.text
    sent = next(value for value in history.json()["data"]["items"] if value["event_id"] == event_id)
    if media:
        attachment = sent["attachments"][0]
        assert attachment["mime_type"] == "image/png" and attachment["size"] == len(content)
        assert attachment["artifact_id"] and attachment["version"]
        assert len(refs) == 1
        files = await env.client.get("/client/v1/artifacts", headers=headers)
        assert files.status_code == 200, files.text
        assert files.json()["data"]["items"][0]["artifact_id"] == attachment["artifact_id"]
        download = await env.client.get(
            f"/client/v1/artifacts/{attachment['artifact_id']}",
            params={"version": attachment["version"]},
            headers=headers,
        )
        assert download.status_code == 200 and download.content == content
        assert "X-Yuki-Expires-At" not in download.headers
        wrong = await env.client.get(
            f"/client/v1/artifacts/{attachment['artifact_id']}",
            params={"version": "0" * 64},
            headers=headers,
        )
        assert wrong.status_code == 404
    else:
        assert sent["text"] == "hello" and not refs


@pytest.mark.parametrize("loss", ["ack", "cancel"])
async def test_local_commit_loss_reconciles_original_outbox(
    app_env, database, tmp_path, monkeypatch, loss
):
    env = app_env
    worker, repository, item, *_ = await prepared(env, database, tmp_path, media=True)
    boundary, settle = database.immediate_session, repository.settle_local_outbox
    armed = False

    async def arm(*args, **kwargs):
        nonlocal armed
        result = await settle(*args, **kwargs)
        armed = result
        return result

    @asynccontextmanager
    async def lose_ack():
        nonlocal armed
        async with boundary() as session:
            yield session
        if armed:
            armed = False
            raise (
                asyncio.CancelledError()
                if loss == "cancel"
                else OSError("lost local commit acknowledgement")
            )

    monkeypatch.setattr(repository, "settle_local_outbox", arm)
    monkeypatch.setattr(database, "immediate_session", lose_ack)
    if loss == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await worker._deliver(item)
    else:
        await worker._deliver(item)
    async with database.read_session() as reader:
        row = await reader.get(PluginNotificationOutboxModel, item.id)
        assert row.status == "sent" and row.event_id is not None
        assert (
            await reader.scalar(
                select(func.count(ChatEventModel.id)).where(ChatEventModel.direction == "outbound")
            )
            == 1
        )
    await worker._deliver(item)
    assert await repository.claim_outbox() is None


async def test_expired_local_lease_never_appends_and_reuses_stable_media(
    app_env, database, tmp_path, monkeypatch
):
    env = app_env
    worker, repository, item, *_ = await prepared(env, database, tmp_path, media=True)
    original = worker._record_outbound

    async def expire(*args, **kwargs):
        async with database.immediate_session() as session:
            row = await session.get(PluginNotificationOutboxModel, item.id)
            row.lease_until = datetime.now(UTC) - timedelta(seconds=1)
        return await original(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(worker, "_record_outbound", expire)
        await worker._deliver(item)
    async with database.read_session() as reader:
        row = await reader.get(PluginNotificationOutboxModel, item.id)
        assert row.event_id is None and row.status == "pending"
        assert (
            await reader.scalar(
                select(func.count(ChatEventModel.id)).where(ChatEventModel.direction == "outbound")
            )
            == 0
        )
    retry = await repository.claim_outbox()
    assert retry.id == item.id and retry.attempts == item.attempts + 1
    await worker._deliver(retry)
    with env.store._transaction(write=False) as db:
        assert db.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == 1
    async with database.read_session() as reader:
        assert (await reader.get(PluginNotificationOutboxModel, item.id)).status == "sent"


async def test_historical_processing_without_local_proof_is_not_replayed(
    app_env, database, tmp_path
):
    _worker, repository, item, *_ = await prepared(app_env, database, tmp_path)
    async with database.immediate_session() as session:
        row = await session.get(PluginNotificationOutboxModel, item.id)
        frozen = json.loads(row.prepared_json)
        frozen["route"].pop("local_ledger")
        frozen["route"].pop("provider_id")
        row.prepared_json = json.dumps(frozen)
        row.lease_until = datetime.now(UTC) - timedelta(seconds=1)
    assert await repository.claim_outbox() is None
    async with database.read_session() as session:
        row = await session.get(PluginNotificationOutboxModel, item.id)
        assert row.status == "uncertain" and row.event_id is None and row.attempts == item.attempts
    assert app_env.calls == []


@pytest.mark.parametrize("reference", [None, "native-late-confirmed"])
@pytest.mark.parametrize("disable_presence", [False, True])
async def test_remote_late_confirmation_ignores_local_preparation_lease(
    database, tmp_path, monkeypatch, reference, disable_presence
):
    env, repository, worker, _ = await prepare_fixture(database, tmp_path)
    await worker._prepare_pending()
    item = await repository.claim_outbox()
    original = worker._transport.send_text

    async def late_ack(**kwargs):
        receipt = await original(**kwargs)
        async with database.immediate_session() as session:
            row = await session.get(PluginNotificationOutboxModel, item.id)
            row.lease_until = datetime.now(UTC) - timedelta(seconds=1)
            if disable_presence:
                presence = await session.get(PresenceModel, item.canonical_presence_id)
                assert presence is not None
                presence.enabled = False
        # A second claimant has already recorded the expired native attempt unknown.
        assert await repository.claim_outbox() is None
        return replace(receipt, message_id=reference)

    monkeypatch.setattr(worker._transport, "send_text", late_ack)
    await worker._deliver(item)
    async with database.read_session() as session:
        row = await session.get(PluginNotificationOutboxModel, item.id)
        assert row.status == "sent" and row.event_id is not None
        event = await session.get(ChatEventModel, row.event_id)
        assert event.native_message_id == reference and event.origin_key == reference
    assert len([action for action, _ in env.bot.calls if action == "send_group_msg"]) == 1


async def test_cancel_before_local_commit_proves_absence_before_retry(
    app_env, database, tmp_path, monkeypatch
):
    worker, repository, item, *_ = await prepared(app_env, database, tmp_path, media=True)

    async def stop(*args, **kwargs):
        raise asyncio.CancelledError

    with monkeypatch.context() as patch:
        patch.setattr(worker, "_record_outbound", stop)
        with pytest.raises(asyncio.CancelledError):
            await worker._deliver(item)
    async with database.read_session() as session:
        row = await session.get(PluginNotificationOutboxModel, item.id)
        assert row.status == "pending" and row.event_id is None
        assert (
            await session.scalar(
                select(func.count(ChatEventModel.id)).where(ChatEventModel.direction == "outbound")
            )
            == 0
        )
    retry = await repository.claim_outbox()
    await worker._deliver(retry)
    async with database.read_session() as session:
        assert (await session.get(PluginNotificationOutboxModel, item.id)).status == "sent"


async def test_grant_revoked_after_media_preparation_rolls_back_local_effect(
    app_env, database, tmp_path, monkeypatch
):
    from yuki.plugin_host.db_models import PluginBackgroundTargetGrantModel

    worker, repository, item, *_ = await prepared(app_env, database, tmp_path, media=True)
    record = worker._record_outbound

    async def revoke(*args, **kwargs):
        async with database.immediate_session() as session:
            grant = await session.scalar(
                select(PluginBackgroundTargetGrantModel).where(
                    PluginBackgroundTargetGrantModel.plugin_id == item.plugin_id,
                    PluginBackgroundTargetGrantModel.canonical_conversation_id
                    == item.canonical_conversation_id,
                )
            )
            grant.enabled = False
        return await record(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(worker, "_record_outbound", revoke)
        await worker._deliver(item)
    retry = await repository.claim_outbox()
    assert retry.id == item.id
    await worker._deliver(retry)
    async with database.read_session() as session:
        row = await session.get(PluginNotificationOutboxModel, item.id)
        assert row.event_id is None and row.status == "cancelled"
        assert (
            await session.scalar(
                select(func.count(ChatEventModel.id)).where(ChatEventModel.direction == "outbound")
            )
            == 0
        )


@pytest.mark.parametrize("change", ["presence_disabled", "binding_ended", "connection_replaced"])
async def test_local_route_change_after_media_preparation_never_publishes(
    app_env, database, tmp_path, monkeypatch, change
):
    worker, _, item, *_ = await prepared(app_env, database, tmp_path, media=True)
    record = worker._record_outbound
    original_route = item.prepared["route"]
    changed = False

    async def change_route(*args, **kwargs):
        nonlocal changed
        if change == "connection_replaced":
            registry = app_env.gateway.registry
            registry.disconnect(app_env.gateway.handle)
            registry.connect(
                app_env.gateway.handle,
                provider_id=original_route["provider_id"],
                presence_id=original_route["presence_id"],
            )
        else:
            async with database.immediate_session() as session:
                if change == "presence_disabled":
                    presence = await session.get(PresenceModel, original_route["presence_id"])
                    assert presence is not None
                    presence.enabled = False
                else:
                    binding = await session.get(IdentityBindingModel, original_route["binding_id"])
                    assert binding is not None
                    binding.status = "ended"
        changed = True
        return await record(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(worker, "_record_outbound", change_route)
        await worker._deliver(item)

    assert changed
    async with database.read_session() as session:
        row = await session.get(PluginNotificationOutboxModel, item.id)
        assert row.status == "pending" and row.event_id is None
        assert row.source_event_id == item.source_event_id and row.attempts == item.attempts
        assert row.platform_message_id is None
        assert json.loads(row.prepared_json) == item.prepared
        assert (
            await session.scalar(
                select(func.count(ChatEventModel.id)).where(ChatEventModel.direction == "outbound")
            )
            == 0
        )
