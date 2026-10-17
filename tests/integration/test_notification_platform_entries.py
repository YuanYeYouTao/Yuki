"""Plugin notifications retain real platform Conversation entries and original effects."""

from datetime import UTC, datetime

import pytest
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
from yuki_plugin_sdk.models import NotificationTarget, PublishNotificationRequest


async def install(database):
    repository = PluginInstallationRepository(database)
    await repository.upsert_discovered(
        plugin_id="native-notification",
        name="Fixture",
        version="1.0",
        plugin_api="4.0",
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
        transport=PlatformNotificationTransport(router=env.social.router),
        effect_gate=ConversationEffectGate(),
    )
    row = await repository.claim_outbox()
    assert row is not None and row.source_event_id == receipts[1].source_event_id
    await worker._deliver(row)
    assert await repository.claim_outbox() is None
    sends = [params for method, params in env.wire if method == "sendMessage"]
    assert len(sends) == 1
    assert sends[0]["chat_id"] == "-10099" and sends[0]["message_thread_id"] == 20
    assert env.model.requests == []
    async with database.sessions() as session:
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
    async with database.sessions() as session:
        await router.validate_prepared_send(
            session, resolved, target_kind="person", target_id=person.person_id
        )
    with pytest.raises(RouteSendError, match="conversation_entry_unavailable"):
        await router.resolve_send_for_conversation(entries[0].conversation_id, presence_id=other.id)
    assert entries[0].conversation_id != entries[1].conversation_id
