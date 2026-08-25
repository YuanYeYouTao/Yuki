"""C23b-2b: canonical notification delivery and plugin background execution."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from tests.conftest import build_harness, make_settings

from qq_ai_bot.conversation.canonical_db_models import (
    ConversationLegacyAliasModel,
    PersonActiveRouteModel,
)
from qq_ai_bot.conversation.hydrate import (
    require_primary_alias_for_conversation,
    synthetic_scope_id,
)
from qq_ai_bot.conversation.rollup.errors import ConversationCoverageError
from qq_ai_bot.conversation.scope import ConversationTurnSnapshot
from qq_ai_bot.domain.conversations import ConversationScope
from qq_ai_bot.gateway.registry import GatewayConnectionRegistry
from qq_ai_bot.identity.db_models import CanonicalPersonModel, IdentityRuntimeStateModel
from qq_ai_bot.identity.dual_write import (
    ensure_canonical_person_preconfig,
    ensure_canonical_space_preconfig,
)
from qq_ai_bot.identity.dual_write import (
    ensure_canonical_presence_preconfig as ensure_v2_presence,
)
from qq_ai_bot.identity.routing import PresenceRouter, RouteSendError
from qq_ai_bot.identity.write_settings import (
    IdentityWriteSettings,
    configure_identity_write_settings,
)
from qq_ai_bot.llm.fake import FakeLLMProvider
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.event_repository import EventLedgerRepository
from qq_ai_bot.persistence.models import ChatEventModel
from qq_ai_bot.plugin_host.background_turns import PluginBackgroundTurnWorker
from qq_ai_bot.plugin_host.db_models import (
    PluginBackgroundTurnJobModel,
    PluginNotificationOutboxModel,
)
from qq_ai_bot.plugin_host.media_artifacts import PluginMediaArtifactStore
from qq_ai_bot.plugin_host.notification_delivery import (
    OneBotNotificationTransport,
    PluginNotificationOutboxWorker,
)
from qq_ai_bot.plugin_host.notification_repository import PluginNotificationRepository
from qq_ai_bot.plugin_host.repository import PluginInstallationRepository
from yuki_plugin_sdk.api import PLUGIN_API_VERSION, is_api_compatible
from yuki_plugin_sdk.models import NotificationTarget, PublishNotificationRequest

_NOW = datetime(2026, 8, 25, tzinfo=UTC)
_CUTOVER = "550e8400-e29b-41d4-a716-446655440099"
PLUGIN_ID = "com.example.c23b2b"


@dataclass
class _Bot:
    self_id: str
    calls: list[tuple[str, dict[str, object]]] = field(default_factory=list)

    async def call_api(self, action: str, **kwargs: object) -> dict[str, object]:
        self.calls.append((action, dict(kwargs)))
        return {"message_id": f"mid-{self.self_id}-{len(self.calls)}"}


class _FakeChat:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def configure_runtime_controls(self, _runtime: object) -> None:
        return None

    async def generate_external_reply(self, **kwargs: object) -> SimpleNamespace:
        self.calls.append(dict(kwargs))
        return SimpleNamespace(text="agent-reply", tool_calls_used=0, model_requests=1)


class _FakeTurns:
    def configure_policy(self, **_kwargs: object) -> None:
        return None

    async def begin_background(self, _key: str) -> SimpleNamespace:
        return SimpleNamespace(version=1)

    @asynccontextmanager
    async def track(self, _token: object, _kind: str):
        yield


class _RecordingChat:
    def __init__(self, inner: object) -> None:
        self._inner = inner
        self.calls: list[dict[str, object]] = []

    def configure_runtime_controls(self, runtime: object) -> None:
        self._inner.configure_runtime_controls(runtime)

    async def generate_external_reply(self, **kwargs: object) -> object:
        self.calls.append(dict(kwargs))
        return await self._inner.generate_external_reply(**kwargs)


class _FakeRuntime:
    async def snapshot(self, **kwargs: object) -> SimpleNamespace:
        del kwargs
        return SimpleNamespace(
            reply=SimpleNamespace(cancel_on_new_message=False),
            conversation_policy=lambda: SimpleNamespace(interrupt_autonomous_on_new_message=False),
        )


async def _true(*_args: object, **_kwargs: object) -> bool:
    return True


async def _install(database: Database, plugin_id: str = PLUGIN_ID) -> None:
    repository = PluginInstallationRepository(database)
    await repository.upsert_discovered(
        plugin_id=plugin_id,
        name="C23b-2b",
        version="1.0.0",
        plugin_api="2.0",
        yuki_requires=">=3.4",
        manifest_hash="b" * 64,
        entrypoint="plugin:Plugin",
        requested_permissions=("notification.publish", "notification.agent"),
    )
    await repository.approve(plugin_id)
    await repository.set_enabled(plugin_id, enabled=True)
    await repository.set_status(plugin_id, status="running")


async def _flip_v2(database: Database) -> None:
    async with database.sessions() as session, session.begin():
        row = await session.get(IdentityRuntimeStateModel, 1)
        assert row is not None
        row.state = "v2"
        row.cutover_id = _CUTOVER
        row.source_fingerprint = "cutover-fingerprint"
        row.completed_at = _NOW


def _publish_request(
    target: NotificationTarget,
    *,
    event_key: str = "evt-1",
    text: str = "hello",
    ask_agent: bool = False,
) -> PublishNotificationRequest:
    return PublishNotificationRequest(
        event_key=event_key,
        event_type="test",
        external_source="test",
        target=target,
        occurred_at=_NOW,
        summary="summary",
        payload={"k": "v"},
        text=text,
        ask_agent=ask_agent,
        agent_intent="reply",
    )


async def _grant_and_publish(
    database: Database,
    *,
    target: NotificationTarget,
    event_key: str,
    ask_agent: bool = False,
) -> PluginNotificationRepository:
    notifications = PluginNotificationRepository(database)
    await notifications.grant_target(
        plugin_id=PLUGIN_ID,
        target=target,
        bot_user_id="8000",
        created_by_user_id="9000",
    )
    await notifications.publish(
        plugin_id=PLUGIN_ID,
        request=_publish_request(target, event_key=event_key, ask_agent=ask_agent),
    )
    return notifications


def test_plugin_api_remains_2_0() -> None:
    assert PLUGIN_API_VERSION == "2.0"
    assert is_api_compatible("2.0")


@pytest.mark.asyncio
async def test_v2_old_outbox_follows_switched_presence_and_keeps_conversation(
    database: Database,
    tmp_path: Path,
) -> None:
    configure_identity_write_settings(IdentityWriteSettings(superusers=frozenset({"9000"})))
    await _install(database)
    await _flip_v2(database)
    async with database.sessions() as session, session.begin():
        await ensure_canonical_person_preconfig(session, "9000", now=_NOW)
        person = await ensure_canonical_person_preconfig(session, "1001", now=_NOW)
        presence_a = await ensure_v2_presence(session, "8000")
        presence_b = await ensure_v2_presence(session, "8001")
    notifications = await _grant_and_publish(
        database,
        target=NotificationTarget(target_type="private", target_id="1001"),
        event_key="route-switch",
    )
    async with database.sessions() as session:
        outbox = await session.scalar(select(PluginNotificationOutboxModel))
        event = await session.scalar(select(ChatEventModel))
        assert outbox is not None and event is not None
        conversation_id = outbox.canonical_conversation_id
        assert conversation_id
        generation, primary = await notifications.conversation_watermark(conversation_id)
        assert event.ingress_presence_id == presence_a
    registry = GatewayConnectionRegistry(gateway_instance_id="gw-c23b2b-person")
    router = PresenceRouter(database, registry, membership_probe=_true)
    bot_a = _Bot("8000")
    bot_b = _Bot("8001")
    registry.connect(bot_a)
    registry.bind_presence(platform="qq", external_account_id="8000", presence_id=presence_a)
    assert await router.cas_takeover_person(person) == "taken"
    registry.connect(bot_b)
    registry.bind_presence(platform="qq", external_account_id="8001", presence_id=presence_b)
    registry.disconnect(bot_a)
    assert await router.cas_takeover_person(person) == "taken"
    ledger = EventLedgerRepository(database)
    worker = PluginNotificationOutboxWorker(
        repository=notifications,
        artifacts=PluginMediaArtifactStore(database, root=tmp_path / "artifacts"),
        ledger=ledger,
        transport=OneBotNotificationTransport(registry, router=router),
    )
    item = await notifications.claim_outbox()
    assert item is not None
    assert item.canonical_target_person_id == person
    await worker._deliver(item)
    assert bot_a.calls == []
    assert bot_b.calls
    assert bot_b.calls[0][0] == "send_private_msg"
    async with database.sessions() as session:
        outbound = (
            await session.scalars(
                select(ChatEventModel)
                .where(ChatEventModel.direction == "outbound")
                .order_by(ChatEventModel.id.desc())
            )
        ).first()
        after = await notifications.conversation_watermark(conversation_id)
    assert outbound is not None
    assert outbound.author_kind == "yuki"
    assert outbound.author_presence_id == presence_b
    assert outbound.ingress_presence_id == presence_b
    assert outbound.canonical_conversation_id == conversation_id
    assert after == (generation, primary)
    async with database.sessions() as session:
        assert await require_primary_alias_for_conversation(session, conversation_id) == primary


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mutate", "category"),
    (
        ("paused", "paused"),
        ("missing", "none"),
        ("ambiguous", "ambiguous"),
    ),
)
async def test_v2_route_paused_missing_ambiguous_retries_without_send(
    database: Database,
    tmp_path: Path,
    mutate: str,
    category: str,
) -> None:
    configure_identity_write_settings(IdentityWriteSettings(superusers=frozenset({"9000"})))
    await _install(database)
    await _flip_v2(database)
    async with database.sessions() as session, session.begin():
        await ensure_canonical_person_preconfig(session, "9000", now=_NOW)
        person = await ensure_canonical_person_preconfig(session, "1001", now=_NOW)
        presence = await ensure_v2_presence(session, "8000")
    notifications = await _grant_and_publish(
        database,
        target=NotificationTarget(target_type="private", target_id="1001"),
        event_key=f"route-{mutate}",
    )
    registry = GatewayConnectionRegistry(gateway_instance_id=f"gw-c23b2b-{mutate}")
    router = PresenceRouter(database, registry, membership_probe=_true)
    bot = _Bot("8000")
    registry.connect(bot)
    registry.bind_presence(platform="qq", external_account_id="8000", presence_id=presence)
    if mutate != "missing":
        assert await router.cas_takeover_person(person) == "taken"
    if mutate == "paused":
        async with database.sessions() as session, session.begin():
            route = await session.get(PersonActiveRouteModel, person)
            assert route is not None
            route.paused = True
    if mutate == "missing":
        registry.disconnect(bot)
    if mutate == "ambiguous":

        async def _ambiguous(_person_id: str) -> object:
            raise RouteSendError("ambiguous")

        router.resolve_send_for_person = _ambiguous  # type: ignore[method-assign]
    worker = PluginNotificationOutboxWorker(
        repository=notifications,
        artifacts=PluginMediaArtifactStore(database, root=tmp_path / "artifacts"),
        ledger=EventLedgerRepository(database),
        transport=OneBotNotificationTransport(registry, router=router),
    )
    item = await notifications.claim_outbox()
    assert item is not None
    await worker._deliver(item)
    assert bot.calls == []
    async with database.sessions() as session:
        row = await session.scalar(select(PluginNotificationOutboxModel))
        outbound = int(
            await session.scalar(
                select(ChatEventModel.id).where(ChatEventModel.direction == "outbound")
            )
            or 0
        )
    assert row is not None
    assert row.status in {"pending", "failed"}
    assert row.last_error_category == category
    assert outbound == 0


@pytest.mark.asyncio
async def test_v2_corrupt_outbox_fields_fail_before_send(
    database: Database,
    tmp_path: Path,
) -> None:
    await _install(database)
    await _flip_v2(database)
    async with database.sessions() as session, session.begin():
        await ensure_canonical_person_preconfig(session, "9000", now=_NOW)
        await ensure_canonical_person_preconfig(session, "1001", now=_NOW)
        await ensure_v2_presence(session, "8000")
    notifications = await _grant_and_publish(
        database,
        target=NotificationTarget(target_type="private", target_id="1001"),
        event_key="corrupt-outbox",
    )
    async with database.sessions() as session, session.begin():
        row = await session.scalar(select(PluginNotificationOutboxModel))
        assert row is not None
        row.canonical_target_person_id = None
        row.canonical_target_space_id = None
    item = await notifications.claim_outbox()
    assert item is None
    async with database.sessions() as session:
        stored = await session.scalar(select(PluginNotificationOutboxModel))
    assert stored is not None
    assert stored.status == "failed"
    assert stored.last_error_category == "canonical_target_missing"


@pytest.mark.asyncio
async def test_v2_disabled_person_blocks_claim_before_send(database: Database) -> None:
    await _install(database)
    await _flip_v2(database)
    async with database.sessions() as session, session.begin():
        await ensure_canonical_person_preconfig(session, "9000", now=_NOW)
        person = await ensure_canonical_person_preconfig(session, "1001", now=_NOW)
        await ensure_v2_presence(session, "8000")
    notifications = await _grant_and_publish(
        database,
        target=NotificationTarget(target_type="private", target_id="1001"),
        event_key="disabled-person",
    )
    async with database.sessions() as session, session.begin():
        row = await session.get(CanonicalPersonModel, person)
        assert row is not None
        row.enabled = False
    item = await notifications.claim_outbox()
    assert item is None
    async with database.sessions() as session:
        stored = await session.scalar(select(PluginNotificationOutboxModel))
    assert stored is not None
    assert stored.status == "failed"
    assert stored.last_error_category == "canonical_owner_disabled"


@pytest.mark.asyncio
async def test_v2_background_paused_retries_without_agent(database: Database) -> None:
    configure_identity_write_settings(IdentityWriteSettings(superusers=frozenset({"9000"})))
    await _install(database)
    await _flip_v2(database)
    async with database.sessions() as session, session.begin():
        await ensure_canonical_person_preconfig(session, "9000", now=_NOW)
        person = await ensure_canonical_person_preconfig(session, "1001", now=_NOW)
        presence = await ensure_v2_presence(session, "8000")
    notifications = await _grant_and_publish(
        database,
        target=NotificationTarget(target_type="private", target_id="1001"),
        event_key="bg-paused",
        ask_agent=True,
    )
    registry = GatewayConnectionRegistry(gateway_instance_id="gw-c23b2b-bg-paused")
    router = PresenceRouter(database, registry, membership_probe=_true)
    bot = _Bot("8000")
    registry.connect(bot)
    registry.bind_presence(platform="qq", external_account_id="8000", presence_id=presence)
    assert await router.cas_takeover_person(person) == "taken"
    async with database.sessions() as session, session.begin():
        route = await session.get(PersonActiveRouteModel, person)
        assert route is not None
        route.paused = True
    chat = _FakeChat()
    worker = PluginBackgroundTurnWorker(
        repository=notifications,
        ledger=EventLedgerRepository(database),
        runtime_config=_FakeRuntime(),  # type: ignore[arg-type]
        chat=chat,  # type: ignore[arg-type]
        turns=_FakeTurns(),  # type: ignore[arg-type]
        conversation_scopes=SimpleNamespace(),  # type: ignore[arg-type]
        router=router,
    )
    job = await notifications.claim_turn()
    assert job is not None
    await worker._execute_admitted(job)
    assert chat.calls == []
    async with database.sessions() as session:
        stored = await session.scalar(select(PluginBackgroundTurnJobModel))
    assert stored is not None
    assert stored.status == "pending"
    assert stored.last_error_category == "paused"


@pytest.mark.asyncio
async def test_v2_finish_turn_inherits_job_canonicals_not_raw_recompute(
    database: Database,
) -> None:
    await _install(database)
    await _flip_v2(database)
    async with database.sessions() as session, session.begin():
        await ensure_canonical_person_preconfig(session, "9000", now=_NOW)
        person = await ensure_canonical_person_preconfig(session, "1001", now=_NOW)
        presence = await ensure_v2_presence(session, "8000")
    notifications = await _grant_and_publish(
        database,
        target=NotificationTarget(target_type="private", target_id="1001"),
        event_key="finish-inherit",
        ask_agent=True,
    )
    async with database.sessions() as session:
        job = await session.scalar(select(PluginBackgroundTurnJobModel))
        event = await session.scalar(select(ChatEventModel))
    assert job is not None and event is not None
    original_person = job.canonical_target_person_id
    original_conversation = job.canonical_conversation_id
    original_presence = job.canonical_presence_id
    async with database.sessions() as session, session.begin():
        stored = await session.get(PluginBackgroundTurnJobModel, job.id)
        assert stored is not None
        stored.target_id = "9999"
        stored.bot_user_id = "0000"
    await notifications.finish_turn(job.id, text="copied", tool_calls_used=0, model_requests=1)
    async with database.sessions() as session:
        reply = await session.scalar(
            select(PluginNotificationOutboxModel).where(
                PluginNotificationOutboxModel.part_key == "agent_reply"
            )
        )
    assert reply is not None
    assert reply.target_id == "9999"
    assert reply.canonical_target_person_id == person
    assert reply.canonical_target_person_id == original_person
    assert reply.canonical_target_space_id is None
    assert reply.canonical_conversation_id == event.canonical_conversation_id
    assert reply.canonical_conversation_id == original_conversation
    assert reply.canonical_presence_id == presence
    assert reply.canonical_presence_id == original_presence


@pytest.mark.asyncio
async def test_v2_background_sdk_uses_job_space_and_actual_presence(
    database: Database,
) -> None:
    configure_identity_write_settings(IdentityWriteSettings(superusers=frozenset({"9000"})))
    await _install(database)
    await _flip_v2(database)
    async with database.sessions() as session, session.begin():
        creator = await ensure_canonical_person_preconfig(session, "9000", now=_NOW)
        space = await ensure_canonical_space_preconfig(session, "2001", now=_NOW)
        presence_a = await ensure_v2_presence(session, "8000")
        presence_b = await ensure_v2_presence(session, "8001")
    notifications = await _grant_and_publish(
        database,
        target=NotificationTarget(target_type="group", target_id="2001"),
        event_key="bg-space",
        ask_agent=True,
    )
    registry = GatewayConnectionRegistry(gateway_instance_id="gw-c23b2b-bg")
    router = PresenceRouter(database, registry, membership_probe=_true)
    bot_a = _Bot("8000")
    bot_b = _Bot("8001")
    registry.connect(bot_a)
    registry.bind_presence(platform="qq", external_account_id="8000", presence_id=presence_a)
    assert await router.cas_takeover_space(space) == "taken"
    registry.connect(bot_b)
    registry.bind_presence(platform="qq", external_account_id="8001", presence_id=presence_b)
    registry.disconnect(bot_a)
    assert await router.cas_takeover_space(space) == "taken"
    chat = _FakeChat()
    worker = PluginBackgroundTurnWorker(
        repository=notifications,
        ledger=EventLedgerRepository(database),
        runtime_config=_FakeRuntime(),  # type: ignore[arg-type]
        chat=chat,  # type: ignore[arg-type]
        turns=_FakeTurns(),  # type: ignore[arg-type]
        conversation_scopes=SimpleNamespace(),  # type: ignore[arg-type]
        router=router,
    )
    job = await notifications.claim_turn()
    assert job is not None
    assert job.canonical_target_space_id == space
    await worker._execute_admitted(job)
    assert len(chat.calls) == 1
    call = chat.calls[0]
    assert call["space_id"] == space
    assert call["person_id"] is None
    assert call["presence_id"] == presence_b
    assert call["conversation_id"] == job.canonical_conversation_id
    assert call["authorization_user_id"] == creator
    turn = call["turn_snapshot"]
    assert isinstance(turn, ConversationTurnSnapshot)
    watermark = await notifications.conversation_watermark(job.canonical_conversation_id or "")
    assert watermark is not None
    assert turn.scope_key == watermark[1]
    assert turn.transport_scope_key == ConversationScope.group("8001", "2001").key


@pytest.mark.asyncio
async def test_v2_background_corrupt_job_fails_before_agent(database: Database) -> None:
    await _install(database)
    await _flip_v2(database)
    async with database.sessions() as session, session.begin():
        await ensure_canonical_person_preconfig(session, "9000", now=_NOW)
        await ensure_canonical_person_preconfig(session, "1001", now=_NOW)
        await ensure_v2_presence(session, "8000")
    notifications = await _grant_and_publish(
        database,
        target=NotificationTarget(target_type="private", target_id="1001"),
        event_key="bg-corrupt",
        ask_agent=True,
    )
    async with database.sessions() as session, session.begin():
        job = await session.scalar(select(PluginBackgroundTurnJobModel))
        assert job is not None
        job.canonical_target_person_id = None
        job.canonical_target_space_id = None
    chat = _FakeChat()
    claimed = await notifications.claim_turn()
    assert claimed is None
    assert chat.calls == []
    async with database.sessions() as session:
        stored = await session.scalar(select(PluginBackgroundTurnJobModel))
    assert stored is not None
    assert stored.status == "failed"
    assert stored.last_error_category == "canonical_target_missing"


@pytest.mark.asyncio
async def test_v1_background_still_uses_raw_grant_and_scope(database: Database) -> None:
    from qq_ai_bot.persistence.people_repository import GroupSettingsRepository, PeopleRepository

    await _install(database)
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
    receipt = await notifications.publish(
        plugin_id=PLUGIN_ID,
        request=_publish_request(target, event_key="v1-bg", ask_agent=True),
    )
    assert receipt.event_created
    chat = _FakeChat()

    class _Scopes:
        async def get(self, scope: object) -> SimpleNamespace:
            return SimpleNamespace(id=1, runtime_scope_key=scope.key, generation=1)

    worker = PluginBackgroundTurnWorker(
        repository=notifications,
        ledger=EventLedgerRepository(database),
        runtime_config=_FakeRuntime(),  # type: ignore[arg-type]
        chat=chat,  # type: ignore[arg-type]
        turns=_FakeTurns(),  # type: ignore[arg-type]
        conversation_scopes=_Scopes(),  # type: ignore[arg-type]
    )
    job = await notifications.claim_turn()
    assert job is not None
    await worker._execute_admitted(job)
    assert len(chat.calls) == 1
    assert "space_id" not in chat.calls[0] or chat.calls[0].get("space_id") is None
    assert chat.calls[0]["authorization_user_id"] == "9000"


@pytest.mark.asyncio
async def test_v1_outbox_still_sends_via_exact_account(
    database: Database,
    tmp_path: Path,
) -> None:
    from qq_ai_bot.persistence.people_repository import PeopleRepository

    await _install(database)
    await PeopleRepository(database).observe(user_id="9000", nickname="Admin")
    await PeopleRepository(database).observe(user_id="1001", nickname="A")
    notifications = PluginNotificationRepository(database)
    target = NotificationTarget(target_type="private", target_id="1001")
    await notifications.grant_target(
        plugin_id=PLUGIN_ID,
        target=target,
        bot_user_id="8000",
        created_by_user_id="9000",
    )
    await notifications.publish(
        plugin_id=PLUGIN_ID,
        request=_publish_request(target, event_key="v1-send"),
    )
    registry = GatewayConnectionRegistry(gateway_instance_id="gw-c23b2b-v1")
    bot = _Bot("8000")
    registry.connect(bot)
    worker = PluginNotificationOutboxWorker(
        repository=notifications,
        artifacts=PluginMediaArtifactStore(database, root=tmp_path / "artifacts"),
        ledger=EventLedgerRepository(database),
        transport=OneBotNotificationTransport(registry),
    )
    item = await notifications.claim_outbox()
    assert item is not None
    await worker._deliver(item)
    assert bot.calls == [("send_private_msg", {"user_id": "1001", "message": "hello"})]


async def _switch_person_presence(
    database: Database,
    *,
    person: str,
    presence_a: str,
    presence_b: str,
    gateway_id: str,
) -> PresenceRouter:
    registry = GatewayConnectionRegistry(gateway_instance_id=gateway_id)
    router = PresenceRouter(database, registry, membership_probe=_true)
    bot_a = _Bot("8000")
    bot_b = _Bot("8001")
    registry.connect(bot_a)
    registry.bind_presence(platform="qq", external_account_id="8000", presence_id=presence_a)
    assert await router.cas_takeover_person(person) == "taken"
    registry.connect(bot_b)
    registry.bind_presence(platform="qq", external_account_id="8001", presence_id=presence_b)
    registry.disconnect(bot_a)
    assert await router.cas_takeover_person(person) == "taken"
    return router


@pytest.mark.asyncio
async def test_v2_background_presence_switch_uses_real_chat_and_current_transport(
    database: Database,
) -> None:
    configure_identity_write_settings(IdentityWriteSettings(superusers=frozenset({"9000"})))
    await _install(database)
    await _flip_v2(database)
    async with database.sessions() as session, session.begin():
        await ensure_canonical_person_preconfig(session, "9000", now=_NOW)
        person = await ensure_canonical_person_preconfig(session, "1001", now=_NOW)
        presence_a = await ensure_v2_presence(session, "8000")
        presence_b = await ensure_v2_presence(session, "8001")
    notifications = await _grant_and_publish(
        database,
        target=NotificationTarget(target_type="private", target_id="1001"),
        event_key="bg-real-switch",
        ask_agent=True,
    )
    async with database.sessions() as session:
        event = await session.scalar(select(ChatEventModel))
        assert event is not None
        conversation_id = event.canonical_conversation_id
        event_bot = event.bot_user_id
        event_presence = event.ingress_presence_id
    assert conversation_id
    before = await notifications.conversation_watermark(conversation_id)
    assert before is not None
    generation, primary = before
    router = await _switch_person_presence(
        database,
        person=person,
        presence_a=presence_a,
        presence_b=presence_b,
        gateway_id="gw-c23b2b-real-chat",
    )
    settings = make_settings(database.url)
    harness = build_harness(
        database,
        settings,
        FakeLLMProvider(lambda _request: "switched-presence-reply"),
    )
    chat = _RecordingChat(harness.processor._chat)
    worker = PluginBackgroundTurnWorker(
        repository=notifications,
        ledger=EventLedgerRepository(database),
        runtime_config=harness.processor._runtime_config,
        chat=chat,  # type: ignore[arg-type]
        turns=harness.processor._turn_coordinator,
        conversation_scopes=harness.conversation_scopes,
        router=router,
    )
    job = await notifications.claim_turn()
    assert job is not None
    await worker._execute_admitted(job)
    assert len(chat.calls) == 1
    call = chat.calls[0]
    turn = call["turn_snapshot"]
    assert isinstance(turn, ConversationTurnSnapshot)
    assert call["conversation_id"] == conversation_id
    assert call["presence_id"] == presence_b
    assert turn.scope_key == primary
    assert turn.transport_scope_key == ConversationScope.private("8001", "1001").key
    async with database.sessions() as session:
        stored = await session.scalar(select(PluginBackgroundTurnJobModel))
        source = await session.get(ChatEventModel, job.source_event_id)
        aliases = list(
            await session.scalars(
                select(ConversationLegacyAliasModel).where(
                    ConversationLegacyAliasModel.conversation_id == conversation_id
                )
            )
        )
        after_primary = await require_primary_alias_for_conversation(session, conversation_id)
    assert stored is not None
    assert stored.status == "completed"
    assert stored.generated_text == "switched-presence-reply"
    assert source is not None
    assert source.bot_user_id == event_bot == "8000"
    assert source.ingress_presence_id == event_presence == presence_a
    assert source.canonical_conversation_id == conversation_id
    assert after_primary == primary
    assert await notifications.conversation_watermark(conversation_id) == (generation, primary)
    assert {row.scope_key for row in aliases} == {
        primary,
        ConversationScope.private("8001", "1001").key,
    }
    assert sum(int(row.is_primary) for row in aliases) == 1


@pytest.mark.asyncio
async def test_v2_assemble_external_wrong_or_missing_snapshot_transport_fails_closed(
    database: Database,
) -> None:
    configure_identity_write_settings(IdentityWriteSettings(superusers=frozenset({"9000"})))
    await _install(database)
    await _flip_v2(database)
    async with database.sessions() as session, session.begin():
        creator = await ensure_canonical_person_preconfig(session, "9000", now=_NOW)
        person = await ensure_canonical_person_preconfig(session, "1001", now=_NOW)
        presence_a = await ensure_v2_presence(session, "8000")
        presence_b = await ensure_v2_presence(session, "8001")
    notifications = await _grant_and_publish(
        database,
        target=NotificationTarget(target_type="private", target_id="1001"),
        event_key="bg-fence-closed",
        ask_agent=True,
    )
    await _switch_person_presence(
        database,
        person=person,
        presence_a=presence_a,
        presence_b=presence_b,
        gateway_id="gw-c23b2b-fence-closed",
    )
    settings = make_settings(database.url)
    harness = build_harness(database, settings, FakeLLMProvider(lambda _request: "unused"))
    event = await EventLedgerRepository(database).get_event(
        (await notifications.claim_turn()).source_event_id  # type: ignore[union-attr]
    )
    assert event is not None
    conversation_id = event.canonical_conversation_id
    assert conversation_id
    watermark = await notifications.conversation_watermark(conversation_id)
    assert watermark is not None
    generation, primary = watermark
    runtime = await harness.processor._runtime_config.snapshot(user_id=creator)
    assembler = harness.processor._chat._context_assembler
    missing = ConversationTurnSnapshot(
        scope_id=synthetic_scope_id(conversation_id),
        scope_key=primary,
        generation=generation,
        trigger_event_id=event.id,
        coordinator_version=1,
    )
    with pytest.raises(ConversationCoverageError, match="snapshot transport"):
        await assembler.assemble_external(
            event=event,
            turn=missing,
            authorization_user_id=creator,
            runtime=runtime,
            agent_intent="reply",
            person_id=person,
            conversation_id=conversation_id,
        )
    wrong = ConversationTurnSnapshot(
        scope_id=synthetic_scope_id(conversation_id),
        scope_key=primary,
        generation=generation,
        trigger_event_id=event.id,
        coordinator_version=1,
        transport_scope_key=ConversationScope.private("8002", "1001").key,
    )
    with pytest.raises(ConversationCoverageError):
        await assembler.assemble_external(
            event=event,
            turn=wrong,
            authorization_user_id=creator,
            runtime=runtime,
            agent_intent="reply",
            person_id=person,
            conversation_id=conversation_id,
        )
    after = await notifications.conversation_watermark(conversation_id)
    assert after == (generation, primary)


@pytest.mark.asyncio
async def test_v1_background_real_chat_still_hydrates_from_event_bot(
    database: Database,
) -> None:
    from qq_ai_bot.persistence.people_repository import PeopleRepository

    await _install(database)
    await PeopleRepository(database).observe(user_id="9000", nickname="Admin")
    await PeopleRepository(database).observe(user_id="1001", nickname="A")
    notifications = PluginNotificationRepository(database)
    target = NotificationTarget(target_type="private", target_id="1001")
    await notifications.grant_target(
        plugin_id=PLUGIN_ID,
        target=target,
        bot_user_id="8000",
        created_by_user_id="9000",
    )
    await notifications.publish(
        plugin_id=PLUGIN_ID,
        request=_publish_request(target, event_key="v1-real-chat", ask_agent=True),
    )
    settings = make_settings(database.url)
    harness = build_harness(
        database,
        settings,
        FakeLLMProvider(lambda _request: "v1-event-bot-reply"),
    )
    chat = _RecordingChat(harness.processor._chat)
    worker = PluginBackgroundTurnWorker(
        repository=notifications,
        ledger=EventLedgerRepository(database),
        runtime_config=harness.processor._runtime_config,
        chat=chat,  # type: ignore[arg-type]
        turns=harness.processor._turn_coordinator,
        conversation_scopes=harness.conversation_scopes,
    )
    job = await notifications.claim_turn()
    assert job is not None
    await worker._execute_admitted(job)
    assert len(chat.calls) == 1
    assert chat.calls[0].get("conversation_id") is None
    turn = chat.calls[0]["turn_snapshot"]
    assert isinstance(turn, ConversationTurnSnapshot)
    assert turn.scope_key == ConversationScope.private("8000", "1001").key
    assert turn.transport_scope_key is None
    async with database.sessions() as session:
        stored = await session.scalar(select(PluginBackgroundTurnJobModel))
        event = await session.scalar(select(ChatEventModel))
    assert stored is not None
    assert stored.status == "completed"
    assert stored.generated_text == "v1-event-bot-reply"
    assert event is not None
    assert event.bot_user_id == "8000"
