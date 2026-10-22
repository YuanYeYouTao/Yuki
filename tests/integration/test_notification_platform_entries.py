"""Plugin notifications retain real platform Conversation entries and original effects."""

import io
from datetime import UTC, datetime

import httpx
import pytest
from PIL import Image
from sqlalchemy import select
from tests.integration.test_telegram_runtime import telegram_env as telegram_env

from yuki.conversation.hydrate import ensure_canonical_conversation
from yuki.gateway.providers.telegram import TelegramClient
from yuki.identity.canonical_repository import (
    create_person_binding,
    create_presence,
    create_space_binding,
    find_identity_binding,
)
from yuki.identity.routing import RouteSendError
from yuki.persistence.models import ChatEventModel
from yuki.plugin_host.db_models import PluginNotificationOutboxModel
from yuki.plugin_host.media_artifacts import PluginMediaArtifactStore
from yuki.plugin_host.notification_delivery import (
    PlatformNotificationTransport,
    PluginNotificationOutboxWorker,
)
from yuki.plugin_host.notification_repository import PluginNotificationRepository
from yuki.plugin_host.repository import PluginInstallationRepository
from yuki.services.effect_gate import ConversationEffectGate
from yuki_plugin_sdk import PLUGIN_API_VERSION
from yuki_plugin_sdk.models import NotificationTarget, PublishNotificationRequest


async def install(database):
    repository = PluginInstallationRepository(database)
    await repository.upsert_discovered(
        plugin_id="native-notification",
        name="Fixture",
        version="1.0",
        plugin_api=PLUGIN_API_VERSION,
        yuki_requires=">=3",
        entrypoint="fixture:Plugin",
        requested_permissions=(),
        manifest_hash="a" * 64,
    )
    await repository.approve("native-notification")
    await repository.set_enabled("native-notification", enabled=True)
    await repository.set_status("native-notification", status="running")
    return PluginNotificationRepository(database)


async def test_two_topics_keep_independent_grant_receipt_and_native_delivery(
    telegram_env, database, tmp_path
):
    env = telegram_env
    now = datetime.now(UTC)
    async with database.immediate_session() as session:
        creator = await find_identity_binding(session, "1001")
        binding = await create_space_binding(
            session,
            external_space_id="-10099",
            name="G",
            enabled=True,
            autonomous_enabled=False,
            require_mention=True,
            now=now,
            platform="telegram",
        )
        conversations = [
            await ensure_canonical_conversation(
                session,
                kind="space",
                space_id=binding.space_id,
                space_binding_id=binding.id,
                thread_id=topic,
                primary_scope_key=f"telegram:bot:80001:group:-10099:thread:{topic}",
            )
            for topic in ("10", "20")
        ]
    repository = await install(database)
    requests = []
    for conversation in conversations:
        target = NotificationTarget(
            target_type="group",
            target_id=binding.space_id,
            conversation_id=conversation.conversation_id,
        )
        await repository.grant_target(
            plugin_id="native-notification",
            target=target,
            bot_user_id="80001",
            created_by_user_id=creator.person_id,
            presence_id=env.gateway.presence_id,
        )
        requests.append(
            PublishNotificationRequest(
                event_key="same-key",
                event_type="fixture",
                external_source="fixture",
                target=target,
                occurred_at=now,
                summary="notification",
                text="hello",
            )
        )
    receipts = [
        await repository.publish(plugin_id="native-notification", request=request)
        for request in requests
    ]
    assert receipts[0].notification_id != receipts[1].notification_id
    assert receipts[0].source_event_id != receipts[1].source_event_id
    await repository.revoke_target(plugin_id="native-notification", target=requests[0].target)
    retried = await repository.publish(plugin_id="native-notification", request=requests[1])
    assert retried.deduplicated and retried.notification_id == receipts[1].notification_id
    worker = PluginNotificationOutboxWorker(
        repository=repository,
        artifacts=PluginMediaArtifactStore(database, tmp_path / "artifacts"),
        ledger=env.harness.ledger,
        transport=PlatformNotificationTransport(database=database, router=env.social.router),
        effect_gate=ConversationEffectGate(),
    )
    await worker._prepare_pending()
    row = await repository.claim_outbox()
    assert row is not None and row.source_event_id == receipts[1].source_event_id
    await worker._deliver(row)
    assert await repository.claim_outbox() is None
    sends = [params for method, params in env.wire if method == "sendMessage"]
    assert len(sends) == 1
    assert sends[0]["chat_id"] == "-10099" and sends[0]["message_thread_id"] == 20
    assert env.model.requests == []
    async with database.read_session() as session:
        outboxes = list(
            await session.scalars(
                select(PluginNotificationOutboxModel).order_by(PluginNotificationOutboxModel.id)
            )
        )
        delivered = await session.scalar(
            select(ChatEventModel).where(ChatEventModel.direction == "outbound")
        )
    assert [row.status for row in outboxes] == ["cancelled", "sent"]
    assert delivered.canonical_conversation_id == conversations[1].conversation_id
    assert delivered.caused_by_event_id == receipts[1].source_event_id
    assert delivered.ingress_presence_id == env.gateway.presence_id


async def test_private_conversation_keeps_original_bot_even_when_other_bot_is_live(
    telegram_env, database
):
    env = telegram_env
    now = datetime.now(UTC)
    second = TelegramClient("second-offline-token", client=env.gateway.client._client)
    second.self_id = "80002"
    async with database.immediate_session() as session:
        person = await create_person_binding(
            session, external_account_id="57", display_name="P", now=now, platform="telegram"
        )
        other = await create_presence(
            session, external_account_id=second.self_id, platform="telegram", now=now
        )
        entries = [
            await ensure_canonical_conversation(
                session,
                kind="private",
                person_id=person.person_id,
                identity_binding_id=person.id,
                presence_id=presence,
                primary_scope_key=f"telegram:bot:{account}:private:57",
            )
            for presence, account in ((env.gateway.presence_id, "80001"), (other.id, "80002"))
        ]
    env.gateway.registry.connect(second, provider_id="telegram", presence_id=other.id)
    router = env.social.router
    resolved = await router.resolve_send_for_conversation(entries[0].conversation_id)
    assert resolved.presence_id == env.gateway.presence_id
    assert resolved.kind == "conversation" and resolved.external_target_id == "57"
    async with database.read_session() as session:
        await router.validate_prepared_send(
            session, resolved, target_kind="person", target_id=person.person_id
        )
    with pytest.raises(RouteSendError, match="conversation_entry_unavailable"):
        await router.resolve_send_for_conversation(entries[0].conversation_id, presence_id=other.id)
    assert entries[0].conversation_id != entries[1].conversation_id


@pytest.mark.parametrize(
    "format,mime,method",
    [
        ("PNG", "image/png", "sendPhoto"),
        ("JPEG", "image/jpeg", "sendPhoto"),
        ("WEBP", "image/webp", "sendPhoto"),
        ("GIF", "image/gif", "sendAnimation"),
    ],
)
async def test_notification_media_keeps_handle_mime_and_original_delivery_fact(
    telegram_env, database, tmp_path, monkeypatch, format, mime, method
):
    env = telegram_env
    now = datetime.now(UTC)
    async with database.immediate_session() as session:
        creator = await find_identity_binding(session, "1001")
        person = await create_person_binding(
            session, external_account_id="57", display_name="P", now=now, platform="telegram"
        )
        conversation = await ensure_canonical_conversation(
            session,
            kind="private",
            person_id=person.person_id,
            identity_binding_id=person.id,
            presence_id=env.gateway.presence_id,
            primary_scope_key="telegram:bot:80001:private:57",
        )
    repository = await install(database)
    target = NotificationTarget(
        target_type="private",
        target_id=person.person_id,
        conversation_id=conversation.conversation_id,
    )
    await repository.grant_target(
        plugin_id="native-notification",
        target=target,
        bot_user_id="80001",
        created_by_user_id=creator.person_id,
        presence_id=env.gateway.presence_id,
    )
    store = PluginMediaArtifactStore(database, tmp_path / "artifacts")
    pixels = io.BytesIO()
    Image.new("RGB", (4, 4), "red").save(pixels, format=format)
    handle = await store.create(
        plugin_id="native-notification",
        data=pixels.getvalue(),
        content_type=mime,
        filename="notification",
        ttl_seconds=3600,
        storage_mb=5,
    )
    request = PublishNotificationRequest(
        event_key="media",
        event_type="fixture",
        external_source="fixture",
        target=target,
        occurred_at=now,
        summary="media",
        media_handles=(handle.handle_id,),
    )
    accepted = await repository.publish(plugin_id="native-notification", request=request)
    wire = []

    def respond(http_request):
        assert http_request.url.path.rsplit("/", 1)[-1] == method
        assert ("Content-Type: " + mime).encode() in http_request.content
        wire.append(http_request)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 99}})

    monkeypatch.setattr(env.gateway.client._client, "_transport", httpx.MockTransport(respond))
    worker = PluginNotificationOutboxWorker(
        repository=repository,
        artifacts=store,
        ledger=env.harness.ledger,
        transport=PlatformNotificationTransport(database=database, router=env.social.router),
        effect_gate=ConversationEffectGate(),
    )
    await worker._prepare_pending()
    item = await repository.claim_outbox()
    await worker._deliver(item)
    duplicate = await repository.publish(plugin_id="native-notification", request=request)
    assert duplicate.deduplicated and duplicate.notification_id == accepted.notification_id
    assert await repository.claim_outbox() is None and len(wire) == 1
    async with database.read_session() as session:
        outbox = await session.get(PluginNotificationOutboxModel, item.id)
        assert outbox.status == "sent"
        sent = await session.scalar(
            select(ChatEventModel).where(ChatEventModel.direction == "outbound")
        )
        assert sent.caused_by_event_id == accepted.source_event_id
        assert sent.canonical_conversation_id == conversation.conversation_id
