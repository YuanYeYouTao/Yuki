"""Host guarantees for plugin external events, Outbox, grants, and artifacts."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select, update

from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.models import ChatEventModel
from qq_ai_bot.persistence.people_repository import GroupSettingsRepository, PeopleRepository
from qq_ai_bot.plugin_host.db_models import (
    PluginBackgroundTurnJobModel,
    PluginMediaArtifactModel,
    PluginNotificationOutboxModel,
)
from qq_ai_bot.plugin_host.media_artifacts import PluginMediaArtifactStore
from qq_ai_bot.plugin_host.notification_repository import PluginNotificationRepository
from qq_ai_bot.plugin_host.repository import PluginInstallationRepository
from yuki_plugin_sdk.errors import PluginPermissionError
from yuki_plugin_sdk.models import NotificationTarget, PublishNotificationRequest

PLUGIN_ID = "test-notifications"


async def _running_plugin(database: Database) -> None:
    repository = PluginInstallationRepository(database)
    await repository.upsert_discovered(
        plugin_id=PLUGIN_ID,
        name="Test",
        version="1.0.0",
        plugin_api="2.0",
        yuki_requires=">=3.4",
        manifest_hash="a" * 64,
        entrypoint="plugin:Plugin",
        requested_permissions=("notification.publish", "notification.agent"),
    )
    await repository.approve(PLUGIN_ID)
    await repository.set_enabled(PLUGIN_ID, enabled=True)
    await repository.set_status(PLUGIN_ID, status="running")


@pytest.mark.asyncio
async def test_publish_is_atomic_idempotent_and_external(database: Database) -> None:
    await _running_plugin(database)
    await PeopleRepository(database).observe(user_id="9000", nickname="Admin")
    await GroupSettingsRepository(database).set_enabled("2001", True)
    notifications = PluginNotificationRepository(database)
    target = NotificationTarget(target_type="group", target_id="2001")
    await notifications.grant_target(
        plugin_id=PLUGIN_ID,
        target=target,
        bot_user_id="9999",
        created_by_user_id="9000",
    )
    request = PublishNotificationRequest(
        event_key="github:owner/repo:PushEvent:1",
        event_type="PushEvent",
        external_source="github",
        target=target,
        occurred_at=datetime.now(UTC),
        summary="owner/repo 推送了一个提交",
        payload={"repository": "owner/repo"},
        text="通知正文",
        ask_agent=True,
        agent_intent="自然回应",
    )

    first = await notifications.publish(plugin_id=PLUGIN_ID, request=request)
    second = await notifications.publish(plugin_id=PLUGIN_ID, request=request)

    assert first.event_created and first.agent_turn_enqueued
    assert second.deduplicated and not second.event_created
    async with database.sessions() as session:
        events = int(await session.scalar(select(func.count(ChatEventModel.id))) or 0)
        outbox = int(
            await session.scalar(select(func.count(PluginNotificationOutboxModel.id))) or 0
        )
        turns = int(await session.scalar(select(func.count(PluginBackgroundTurnJobModel.id))) or 0)
        event = await session.get(ChatEventModel, first.source_event_id)
    assert (events, outbox, turns) == (1, 1, 1)
    assert event is not None
    assert event.direction == "external"
    assert event.origin == "plugin_background"
    assert event.sender_user_id == "9999"


@pytest.mark.asyncio
async def test_revoked_target_rejects_new_publication(database: Database) -> None:
    await _running_plugin(database)
    await PeopleRepository(database).observe(user_id="9000", nickname="")
    await GroupSettingsRepository(database).set_enabled("2001", True)
    notifications = PluginNotificationRepository(database)
    target = NotificationTarget(target_type="group", target_id="2001")
    await notifications.grant_target(
        plugin_id=PLUGIN_ID,
        target=target,
        bot_user_id="9999",
        created_by_user_id="9000",
    )
    assert await notifications.revoke_target(plugin_id=PLUGIN_ID, target=target)
    with pytest.raises(PluginPermissionError, match="not granted"):
        await notifications.publish(
            plugin_id=PLUGIN_ID,
            request=PublishNotificationRequest(
                event_key="event-2",
                event_type="test",
                external_source="test",
                target=target,
                occurred_at=datetime.now(UTC),
                summary="test",
            ),
        )


@pytest.mark.asyncio
async def test_media_artifact_is_opaque_bounded_and_plugin_scoped(
    database: Database,
    tmp_path: Path,
) -> None:
    await _running_plugin(database)
    store = PluginMediaArtifactStore(database, root=tmp_path / "artifacts")
    png = b"\x89PNG\r\n\x1a\n" + b"bounded-test"
    handle = await store.create(
        plugin_id=PLUGIN_ID,
        data=png,
        content_type="image/png",
        filename="card.png",
        ttl_seconds=60,
        storage_mb=1,
    )
    resolved = await store.resolve(plugin_id=PLUGIN_ID, handle_id=handle.handle_id)
    assert await asyncio.to_thread(resolved.local_path.read_bytes) == png
    assert str(resolved.local_path) not in handle.model_dump_json()
    with pytest.raises(PluginPermissionError, match="foreign"):
        await store.resolve(plugin_id="other-plugin", handle_id=handle.handle_id)


@pytest.mark.asyncio
async def test_completed_media_can_expire_without_breaking_publish_idempotency(
    database: Database,
    tmp_path: Path,
) -> None:
    await _running_plugin(database)
    await PeopleRepository(database).observe(user_id="9000", nickname="Admin")
    await GroupSettingsRepository(database).set_enabled("2001", True)
    notifications = PluginNotificationRepository(database)
    target = NotificationTarget(target_type="group", target_id="2001")
    await notifications.grant_target(
        plugin_id=PLUGIN_ID,
        target=target,
        bot_user_id="9999",
        created_by_user_id="9000",
    )
    store = PluginMediaArtifactStore(database, root=tmp_path / "artifacts")
    handle = await store.create(
        plugin_id=PLUGIN_ID,
        data=b"\x89PNG\r\n\x1a\nsmall",
        content_type="image/png",
        filename="card.png",
        ttl_seconds=60,
        storage_mb=1,
    )
    request = PublishNotificationRequest(
        event_key="media-event",
        event_type="PushEvent",
        external_source="github",
        target=target,
        occurred_at=datetime.now(UTC),
        summary="push",
        media_handles=(handle.handle_id,),
    )
    await notifications.publish(plugin_id=PLUGIN_ID, request=request)
    item = await notifications.claim_outbox()
    assert item is not None
    await notifications.finish_outbox(item.id, status="sent", platform_message_id="123")
    async with database.sessions() as session, session.begin():
        await session.execute(
            update(PluginMediaArtifactModel)
            .where(PluginMediaArtifactModel.handle_id == handle.handle_id)
            .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    assert await store.cleanup() == 1

    receipt = await notifications.publish(plugin_id=PLUGIN_ID, request=request)

    assert receipt.deduplicated
    assert not receipt.delivery_enqueued


async def _true(*_args: object, **_kwargs: object) -> bool:
    return True


async def _flip_v2(database: Database) -> None:
    from qq_ai_bot.identity.db_models import IdentityRuntimeStateModel

    async with database.sessions() as session, session.begin():
        row = await session.get(IdentityRuntimeStateModel, 1)
        assert row is not None
        row.state = "v2"
        row.cutover_id = "550e8400-e29b-41d4-a716-446655440099"
        row.source_fingerprint = "cutover-fingerprint"
        row.completed_at = datetime(2026, 8, 24, tzinfo=UTC)


@pytest.mark.asyncio
async def test_outbox_delivers_persisted_person_after_legacy_remap(
    database: Database,
    tmp_path: Path,
) -> None:
    from uuid import uuid4

    from qq_ai_bot.gateway.registry import GatewayConnectionRegistry
    from qq_ai_bot.identity.db_models import IdentityBindingModel
    from qq_ai_bot.identity.dual_write import sync_account
    from qq_ai_bot.identity.ingress import ensure_v2_presence
    from qq_ai_bot.identity.routing import PresenceRouter
    from qq_ai_bot.identity.shadows import person_id_for
    from qq_ai_bot.identity.write_settings import (
        IdentityWriteSettings,
        configure_identity_write_settings,
    )
    from qq_ai_bot.persistence.models import PersonModel
    from qq_ai_bot.plugin_host.notification_delivery import (
        OneBotNotificationTransport,
        PluginNotificationOutboxWorker,
    )

    configure_identity_write_settings(
        IdentityWriteSettings(superusers=frozenset({"1001", "1002", "9000"}))
    )
    await _running_plugin(database)
    await PeopleRepository(database).observe(user_id="9000", nickname="Admin")
    await PeopleRepository(database).observe(user_id="1001", nickname="A")
    await PeopleRepository(database).observe(user_id="1002", nickname="B")
    now = datetime.now(UTC)
    async with database.sessions() as session, session.begin():
        await sync_account(session, "1001", role="human", now=now)
        await sync_account(session, "1002", role="human", now=now)
        person_a = await person_id_for(session, "1001")
        person_b = await person_id_for(session, "1002")
    assert person_a and person_b and person_a != person_b
    notifications = PluginNotificationRepository(database)
    target = NotificationTarget(target_type="private", target_id="1001")
    await notifications.grant_target(
        plugin_id=PLUGIN_ID,
        target=target,
        bot_user_id="8001",
        created_by_user_id="9000",
    )
    await notifications.publish(
        plugin_id=PLUGIN_ID,
        request=PublishNotificationRequest(
            event_key="person-persist",
            event_type="test",
            external_source="test",
            target=target,
            occurred_at=now,
            summary="persist A",
            text="hello-A",
        ),
    )
    async with database.sessions() as session:
        stored = await session.scalar(select(PluginNotificationOutboxModel))
    assert stored is not None
    assert stored.target_id == "1001"
    assert stored.canonical_target_person_id == person_a
    assert stored.canonical_target_space_id is None
    await _flip_v2(database)
    registry = GatewayConnectionRegistry(gateway_instance_id="gw-outbox-person")
    router = PresenceRouter(database, registry, membership_probe=_true)

    class _RecordBot:
        def __init__(self, self_id: str) -> None:
            self.self_id = self_id
            self.calls: list[tuple[str, dict[str, object]]] = []

        async def call_api(self, action: str, **kwargs: object) -> dict[str, object]:
            self.calls.append((action, dict(kwargs)))
            return {"message_id": "persist-person"}

    bot = _RecordBot("8001")
    async with database.sessions() as session, session.begin():
        presence = await ensure_v2_presence(session, "8001")
        binding_a = await session.scalar(
            select(IdentityBindingModel).where(IdentityBindingModel.person_id == person_a)
        )
        assert binding_a is not None
        binding_a.external_account_id = "1009"
        people = await session.get(PersonModel, "1001")
        assert people is not None
        people.canonical_person_id = person_b
        session.add(
            IdentityBindingModel(
                id=str(uuid4()),
                person_id=person_b,
                platform="qq",
                external_account_id="1001",
                display_name="moved",
                status="active",
                revision=1,
                created_at=now,
                updated_at=now,
            )
        )
    registry.connect(bot)
    registry.bind_presence(platform="qq", external_account_id="8001", presence_id=presence)
    assert await router.cas_takeover_person(person_a) == "taken"
    ledger: list[dict[str, object]] = []

    class _Ledger:
        async def get_event(self, _event_id: int) -> None:
            return None

        async def append(self, **kwargs: object) -> None:
            ledger.append(dict(kwargs))

    worker = PluginNotificationOutboxWorker(
        repository=notifications,
        artifacts=PluginMediaArtifactStore(database, root=tmp_path / "artifacts"),
        ledger=_Ledger(),  # type: ignore[arg-type]
        transport=OneBotNotificationTransport(registry, router=router),
    )
    item = await notifications.claim_outbox()
    assert item is not None
    assert item.canonical_target_person_id == person_a
    await worker._deliver(item)
    assert bot.calls == [("send_private_msg", {"user_id": "1009", "message": "hello-A"})]
    assert ledger[-1]["private_peer_user_id"] == "1009"
    assert ledger[-1]["bot_user_id"] == "8001"
    async with database.sessions() as session:
        assert await person_id_for(session, "1001") == person_b


@pytest.mark.asyncio
async def test_outbox_delivers_persisted_space_after_legacy_remap(
    database: Database,
    tmp_path: Path,
) -> None:
    from uuid import uuid4

    from qq_ai_bot.gateway.registry import GatewayConnectionRegistry
    from qq_ai_bot.identity.db_models import SpaceBindingModel
    from qq_ai_bot.identity.dual_write import sync_space
    from qq_ai_bot.identity.ingress import ensure_v2_presence
    from qq_ai_bot.identity.routing import PresenceRouter
    from qq_ai_bot.identity.shadows import space_id_for
    from qq_ai_bot.identity.write_settings import (
        IdentityWriteSettings,
        configure_identity_write_settings,
    )
    from qq_ai_bot.persistence.models import GroupModel
    from qq_ai_bot.plugin_host.notification_delivery import (
        OneBotNotificationTransport,
        PluginNotificationOutboxWorker,
    )

    configure_identity_write_settings(IdentityWriteSettings(superusers=frozenset({"9000"})))
    await _running_plugin(database)
    await PeopleRepository(database).observe(user_id="9000", nickname="Admin")
    await GroupSettingsRepository(database).set_enabled("2001", True)
    await GroupSettingsRepository(database).set_enabled("2002", True)
    now = datetime.now(UTC)
    async with database.sessions() as session, session.begin():
        await sync_space(session, "2001", enabled=True, now=now)
        await sync_space(session, "2002", enabled=True, now=now)
        space_a = await space_id_for(session, "2001")
        space_b = await space_id_for(session, "2002")
    assert space_a and space_b and space_a != space_b
    notifications = PluginNotificationRepository(database)
    target = NotificationTarget(target_type="group", target_id="2001")
    await notifications.grant_target(
        plugin_id=PLUGIN_ID,
        target=target,
        bot_user_id="8001",
        created_by_user_id="9000",
    )
    await notifications.publish(
        plugin_id=PLUGIN_ID,
        request=PublishNotificationRequest(
            event_key="space-persist",
            event_type="test",
            external_source="test",
            target=target,
            occurred_at=now,
            summary="persist space",
            text="hello-space",
        ),
    )
    async with database.sessions() as session:
        stored = await session.scalar(select(PluginNotificationOutboxModel))
    assert stored is not None
    assert stored.canonical_target_space_id == space_a
    assert stored.canonical_target_person_id is None
    await _flip_v2(database)
    async with database.sessions() as session, session.begin():
        binding_a = await session.scalar(
            select(SpaceBindingModel).where(SpaceBindingModel.space_id == space_a)
        )
        assert binding_a is not None
        binding_a.external_space_id = "2009"
        group = await session.get(GroupModel, "2001")
        assert group is not None
        group.canonical_space_id = space_b
        session.add(
            SpaceBindingModel(
                id=str(uuid4()),
                space_id=space_b,
                platform="qq",
                external_space_id="2001",
                display_name="moved",
                status="active",
                revision=1,
                created_at=now,
                updated_at=now,
            )
        )
        presence = await ensure_v2_presence(session, "8001")
    registry = GatewayConnectionRegistry(gateway_instance_id="gw-outbox-space")
    router = PresenceRouter(database, registry, membership_probe=_true)

    class _RecordBot:
        def __init__(self, self_id: str) -> None:
            self.self_id = self_id
            self.calls: list[tuple[str, dict[str, object]]] = []

        async def call_api(self, action: str, **kwargs: object) -> dict[str, object]:
            self.calls.append((action, dict(kwargs)))
            return {"message_id": "persist-space"}

    bot = _RecordBot("8001")
    registry.connect(bot)
    registry.bind_presence(platform="qq", external_account_id="8001", presence_id=presence)
    assert await router.cas_takeover_space(space_a) == "taken"
    ledger: list[dict[str, object]] = []

    class _Ledger:
        async def get_event(self, _event_id: int) -> None:
            return None

        async def append(self, **kwargs: object) -> None:
            ledger.append(dict(kwargs))

    worker = PluginNotificationOutboxWorker(
        repository=notifications,
        artifacts=PluginMediaArtifactStore(database, root=tmp_path / "artifacts"),
        ledger=_Ledger(),  # type: ignore[arg-type]
        transport=OneBotNotificationTransport(registry, router=router),
    )
    item = await notifications.claim_outbox()
    assert item is not None
    assert item.canonical_target_space_id == space_a
    await worker._deliver(item)
    assert bot.calls == [("send_group_msg", {"group_id": "2009", "message": "hello-space"})]
    assert ledger[-1]["group_id"] == "2009"
    async with database.sessions() as session:
        assert await space_id_for(session, "2001") == space_b


@pytest.mark.asyncio
async def test_v2_outbox_without_canonical_shadows_fails_closed(
    database: Database,
    tmp_path: Path,
) -> None:
    from qq_ai_bot.plugin_host.notification_delivery import (
        NotificationDeliveryReceipt,
        PluginNotificationOutboxWorker,
    )

    await _running_plugin(database)
    await PeopleRepository(database).observe(user_id="9000", nickname="Admin")
    await GroupSettingsRepository(database).set_enabled("2001", True)
    notifications = PluginNotificationRepository(database)
    target = NotificationTarget(target_type="group", target_id="2001")
    await notifications.grant_target(
        plugin_id=PLUGIN_ID,
        target=target,
        bot_user_id="9999",
        created_by_user_id="9000",
    )
    await notifications.publish(
        plugin_id=PLUGIN_ID,
        request=PublishNotificationRequest(
            event_key="missing-shadow",
            event_type="test",
            external_source="test",
            target=target,
            occurred_at=datetime.now(UTC),
            summary="missing",
            text="should-not-send",
        ),
    )
    async with database.sessions() as session, session.begin():
        row = await session.scalar(select(PluginNotificationOutboxModel))
        assert row is not None
        row.canonical_target_person_id = None
        row.canonical_target_space_id = None
    await _flip_v2(database)

    class _Capture:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        async def send_text(self, **kwargs: object) -> NotificationDeliveryReceipt:
            self.calls.append(dict(kwargs))
            return NotificationDeliveryReceipt(
                message_id="nope",
                sender_account_id="9999",
                external_target_id="2001",
                route_kind="account",
            )

    transport = _Capture()

    class _Ledger:
        async def get_event(self, _event_id: int) -> None:
            return None

        async def append(self, **_kwargs: object) -> None:
            raise AssertionError("must not record a failed-closed send")

    worker = PluginNotificationOutboxWorker(
        repository=notifications,
        artifacts=PluginMediaArtifactStore(database, root=tmp_path / "artifacts"),
        ledger=_Ledger(),  # type: ignore[arg-type]
        transport=transport,  # type: ignore[arg-type]
    )
    item = await notifications.claim_outbox()
    assert item is not None
    await worker._deliver(item)
    assert transport.calls == []
    async with database.sessions() as session:
        row = await session.scalar(select(PluginNotificationOutboxModel))
    assert row is not None
    assert row.status == "failed"
    assert row.last_error_category == "canonical_target_missing"


@pytest.mark.asyncio
async def test_v2_grant_and_publish_use_active_bindings_without_people(
    database: Database,
) -> None:
    from qq_ai_bot.identity.dual_write import ensure_canonical_person_preconfig
    from qq_ai_bot.identity.shadows import active_person_id_for

    await _running_plugin(database)
    await _flip_v2(database)
    now = datetime.now(UTC)
    async with database.sessions() as session, session.begin():
        creator = await ensure_canonical_person_preconfig(session, "9000", now=now)
        target = await ensure_canonical_person_preconfig(session, "1001", now=now)
    notifications = PluginNotificationRepository(database)
    grant_target = NotificationTarget(target_type="private", target_id="1001")
    with pytest.raises(PluginPermissionError, match="unknown"):
        await notifications.grant_target(
            plugin_id=PLUGIN_ID,
            target=NotificationTarget(target_type="private", target_id="404"),
            bot_user_id="8001",
            created_by_user_id="9000",
        )
    await notifications.grant_target(
        plugin_id=PLUGIN_ID,
        target=grant_target,
        bot_user_id="8001",
        created_by_user_id="9000",
    )
    receipt = await notifications.publish(
        plugin_id=PLUGIN_ID,
        request=PublishNotificationRequest(
            event_key="v2-canonical-only",
            event_type="test",
            external_source="test",
            target=grant_target,
            occurred_at=now,
            summary="v2 publish",
            text="hello-v2",
        ),
    )
    assert receipt.event_created
    assert receipt.delivery_enqueued
    async with database.sessions() as session:
        from qq_ai_bot.identity.db_models import CanonicalPersonModel, IdentityBindingModel

        persons = int(
            await session.scalar(select(func.count()).select_from(CanonicalPersonModel)) or 0
        )
        bindings = int(
            await session.scalar(select(func.count()).select_from(IdentityBindingModel)) or 0
        )
        outbox = await session.scalar(select(PluginNotificationOutboxModel))
        event = await session.scalar(
            select(ChatEventModel).where(ChatEventModel.external_event_key == "v2-canonical-only")
        )
        assert await active_person_id_for(session, "9000") == creator
        assert await active_person_id_for(session, "1001") == target
    assert persons == 2
    assert bindings == 2
    assert outbox is not None
    assert outbox.canonical_target_person_id == target
    assert outbox.canonical_target_space_id is None
    assert event is not None
    assert event.author_kind == "system"
    assert event.author_person_id is None


@pytest.mark.asyncio
async def test_v2_grant_unknown_disabled_and_missing_creator_fail_closed(
    database: Database,
) -> None:
    from qq_ai_bot.identity.db_models import IdentityBindingModel
    from qq_ai_bot.identity.dual_write import (
        ensure_canonical_person_preconfig,
        ensure_canonical_space_preconfig,
    )
    from qq_ai_bot.persistence.models import GroupModel, PersonModel

    await _running_plugin(database)
    await _flip_v2(database)
    now = datetime.now(UTC)
    async with database.sessions() as session, session.begin():
        await ensure_canonical_person_preconfig(session, "9000", now=now)
        await ensure_canonical_person_preconfig(session, "1001", now=now)
        await ensure_canonical_space_preconfig(session, "2001", now=now)
        binding = await session.scalar(
            select(IdentityBindingModel).where(IdentityBindingModel.external_account_id == "1001")
        )
        assert binding is not None
        binding.status = "disabled"
    notifications = PluginNotificationRepository(database)
    with pytest.raises(PluginPermissionError, match="grant creator is not a known person"):
        await notifications.grant_target(
            plugin_id=PLUGIN_ID,
            target=NotificationTarget(target_type="private", target_id="1001"),
            bot_user_id="8001",
            created_by_user_id="404",
        )
    with pytest.raises(PluginPermissionError, match="unknown"):
        await notifications.grant_target(
            plugin_id=PLUGIN_ID,
            target=NotificationTarget(target_type="private", target_id="1001"),
            bot_user_id="8001",
            created_by_user_id="9000",
        )
    with pytest.raises(PluginPermissionError, match="unknown or disabled"):
        await notifications.grant_target(
            plugin_id=PLUGIN_ID,
            target=NotificationTarget(target_type="group", target_id="404"),
            bot_user_id="8001",
            created_by_user_id="9000",
        )
    async with database.sessions() as session:
        people = int(await session.scalar(select(func.count()).select_from(PersonModel)) or 0)
        groups = int(await session.scalar(select(func.count()).select_from(GroupModel)) or 0)
    assert people == 0
    assert groups == 0
