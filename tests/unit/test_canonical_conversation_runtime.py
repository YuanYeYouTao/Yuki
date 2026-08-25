"""C19-C24 canonical conversation, memory, automation, plugin, and scope."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from tests.support.gateway import napcat_registry

from qq_ai_bot.automation.models import TurnOrigin
from qq_ai_bot.conversation.canonical_db_models import ConversationLegacyAliasModel
from qq_ai_bot.conversation.scope import ConversationTurnSnapshot, turn_matches_hydrated_scope
from qq_ai_bot.domain.conversations import ConversationScope, ScopeType
from qq_ai_bot.domain.messages import InboundMessage, SenderIdentity
from qq_ai_bot.identity.canonical_uow import CanonicalIngressUnitOfWork
from qq_ai_bot.identity.db_models import IdentityRuntimeStateModel
from qq_ai_bot.identity.dual_write import ensure_canonical_presence_preconfig as ensure_v2_presence
from qq_ai_bot.identity.ingress import CanonicalIngressResolver
from qq_ai_bot.identity.inventory import (
    CUTOVER_BASELINE_PENDING,
    DEFERRED_SHADOWS,
    FILLABLE_SHADOWS,
    LEGACY_PROVENANCE_RETAINED,
)
from qq_ai_bot.identity.routing import PresenceRouter
from qq_ai_bot.identity.write_settings import (
    IdentityWriteSettings,
    configure_identity_write_settings,
)
from qq_ai_bot.memory.enums import (
    MemoryKind,
    MemoryScopeType,
    MemorySourceType,
    SelfMemoryVisibility,
)
from qq_ai_bot.memory.models import MemoryFactCreate
from qq_ai_bot.memory.repository import MemoryFactRepository, MemoryJobRepository
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.models import ChatEventModel, MemoryFactModel
from qq_ai_bot.plugin_host.facades import PluginInvocation
from qq_ai_bot.plugin_host.repository import PluginStateRepository
from qq_ai_bot.runtime.keys import ResolvedMemoryScope
from yuki_plugin_sdk.api import PLUGIN_API_VERSION
from yuki_plugin_sdk.models import CurrentMessage

_NOW = datetime(2026, 8, 24, tzinfo=UTC)
_CUTOVER = "550e8400-e29b-41d4-a716-446655440099"


@dataclass
class _Bot:
    self_id: str

    async def call_api(self, *_args: object, **_kwargs: object) -> dict[str, object]:
        return {}


async def _flip_v2(database: Database) -> None:
    async with database.sessions() as session, session.begin():
        row = await session.get(IdentityRuntimeStateModel, 1)
        assert row is not None
        row.state = "v2"
        row.cutover_id = _CUTOVER
        row.source_fingerprint = "cutover-fingerprint"
        row.completed_at = _NOW


async def _true(*_args: object, **_kwargs: object) -> bool:
    return True


def _private(message_id: str, user_id: str = "1001", bot_user_id: str = "8000") -> InboundMessage:
    return InboundMessage(
        message_id=message_id,
        event_type="message:test",
        scope_type=ScopeType.PRIVATE,
        sender=SenderIdentity(user_id=user_id),
        text="hi",
        bot_user_id=bot_user_id,
    )


@pytest.mark.asyncio
async def test_primary_alias_freezes_and_generation_only_on_new(database: Database) -> None:
    from qq_ai_bot.identity.ingress import _ensure_person_id

    configure_identity_write_settings(IdentityWriteSettings(superusers=frozenset({"9000"})))
    await _flip_v2(database)
    registry = napcat_registry(gateway_instance_id="gw-conv")
    router = PresenceRouter(database, registry, membership_probe=_true)
    resolver = CanonicalIngressResolver(database, registry, router)
    uow = CanonicalIngressUnitOfWork(database, router)
    bot_a = _Bot("8000")
    bot_b = _Bot("8001")
    async with database.sessions() as session, session.begin():
        presence_a = await ensure_v2_presence(session, "8000")
        presence_b = await ensure_v2_presence(session, "8001")
        await _ensure_person_id(session, "1001")
    registry.connect(bot_a)
    registry.connect(bot_b)
    registry.bind_presence(platform="qq", external_account_id="8000", presence_id=presence_a)
    registry.bind_presence(platform="qq", external_account_id="8001", presence_id=presence_b)
    first = await resolver.pre_admit(bot_a, _private("c1"))
    assert first is not None and first.primary_alias
    primary = first.primary_alias
    second = await resolver.pre_admit(bot_b, _private("c2", bot_user_id="8001"))
    assert second is not None
    assert second.conversation_id == first.conversation_id
    assert second.primary_alias == primary
    appended = await uow.append_inbound(first.message, first)
    assert appended.scope.generation == 1
    switched = await uow.append_new_generation(_private("c-new"), first)
    assert switched.scope.generation == 2
    async with database.sessions() as session:
        aliases = list(
            await session.scalars(
                select(ConversationLegacyAliasModel).where(
                    ConversationLegacyAliasModel.conversation_id == first.conversation_id
                )
            )
        )
        primaries = [item.scope_key for item in aliases if int(item.is_primary) == 1]
        assert primaries == [primary]


@pytest.mark.asyncio
async def test_memory_partition_stays_off_conversation_id_and_refuses_legacy_replay(
    database: Database,
) -> None:
    configure_identity_write_settings(IdentityWriteSettings(superusers=frozenset({"9000"})))
    partition = ResolvedMemoryScope.for_private("1001").partition_key
    assert partition.startswith("private:")
    assert "conversation" not in partition
    await _flip_v2(database)
    jobs = MemoryJobRepository(database)
    facts = MemoryFactRepository(database)
    async with database.sessions() as session, session.begin():
        from qq_ai_bot.identity.ingress import _ensure_person_id

        await _ensure_person_id(session, "1001")
    refused = await jobs.enqueue(999999, partition)
    assert refused is False
    async with database.sessions() as session, session.begin():
        from qq_ai_bot.identity.ingress import _ensure_person_id

        person_id = await _ensure_person_id(session, "1001")
        row = await facts.create_fact(
            MemoryFactCreate(
                scope_type=MemoryScopeType.PERSON,
                subject_user_id="1001",
                kind=MemoryKind.FACT,
                memory_key="pref.color",
                category="preference",
                content="blue",
                source_type=MemorySourceType.EXPLICIT,
            ),
            normalized_content="blue",
            supersedes_id=None,
            session=session,
        )
        stored = await session.get(MemoryFactModel, row.id)
        assert stored is not None
        assert stored.canonical_subject_person_id == person_id
        self_row = await facts.create_fact(
            MemoryFactCreate(
                scope_type=MemoryScopeType.SELF,
                kind=MemoryKind.FACT,
                memory_key="self.style",
                category="self_preference",
                content="calm",
                source_type=MemorySourceType.EXPLICIT,
                visibility_type=SelfMemoryVisibility.PRIVATE,
                visibility_user_id="1001",
            ),
            normalized_content="calm",
            supersedes_id=None,
            session=session,
        )
        self_stored = await session.get(MemoryFactModel, self_row.id)
        assert self_stored is not None
        assert self_stored.canonical_subject_person_id is None
        assert self_stored.canonical_visibility_person_id == person_id
    async with database.sessions() as session, session.begin():
        await ensure_v2_presence(session, "8001")
    async with database.sessions() as session:
        again = await session.get(MemoryFactModel, self_row.id)
        assert again is not None
        assert again.canonical_visibility_person_id == person_id
        assert again.canonical_subject_person_id is None


@pytest.mark.asyncio
async def test_plugin_api_remains_2_0_and_host_state_uses_primary_alias() -> None:
    assert PLUGIN_API_VERSION == "2.0"
    now = datetime.now(UTC)
    current = CurrentMessage(
        message_id="1",
        sender_user_id="1001",
        scope_type="private",
        received_at=now,
    )
    assert current.person_id is None
    inbound = InboundMessage(
        message_id="1",
        event_type="message:test",
        scope_type=ScopeType.PRIVATE,
        sender=SenderIdentity(user_id="1001"),
        text="hi",
        bot_user_id="8000",
        legacy_conversation_key="private:8000:1001",
        person_id="person-1",
    )
    invocation = PluginInvocation(
        plugin_id="demo.plugin",
        origin=TurnOrigin.USER_MESSAGE,
        actor_user_id="1001",
        bot_user_id="8000",
        inbound=inbound,
    )
    assert invocation.conversation_key == "private:8000:1001"


def test_plugin_command_adapter_uses_primary_legacy_key() -> None:
    from qq_ai_bot.conversation.scope import plugin_conversation_key
    from qq_ai_bot.domain.conversations import ConversationScope

    inbound = InboundMessage(
        message_id="cmd-1",
        event_type="message:test",
        scope_type=ScopeType.PRIVATE,
        sender=SenderIdentity(user_id="1001"),
        text="/ai plugin run demo ping",
        bot_user_id="8001",
        legacy_conversation_key="bot:8000:private:1001",
    )
    identity = ConversationScope.private("8001", "1001")
    assert plugin_conversation_key(inbound, identity) == "bot:8000:private:1001"
    v1_identity = ConversationScope.private("8001", "1001")
    v1_message = InboundMessage(
        message_id="cmd-v1",
        event_type="message:test",
        scope_type=ScopeType.PRIVATE,
        sender=SenderIdentity(user_id="1001"),
        text="hi",
        bot_user_id="8001",
    )
    assert plugin_conversation_key(v1_message, v1_identity) == v1_identity.key


def test_runtime_conversation_key_fails_closed_when_v2_primary_missing() -> None:
    from qq_ai_bot.conversation.scope import runtime_conversation_key
    from qq_ai_bot.domain.conversations import ConversationScope

    inbound = InboundMessage(
        message_id="missing-primary",
        event_type="message:test",
        scope_type=ScopeType.PRIVATE,
        sender=SenderIdentity(user_id="1001"),
        text="hi",
        bot_user_id="8001",
        conversation_id="conv-1",
    )
    identity = ConversationScope.private("8001", "1001")
    with pytest.raises(ValueError, match="missing primary runtime key"):
        runtime_conversation_key(identity=identity, inbound=inbound)


def test_plugin_facade_uses_inbound_legacy_key_and_fails_closed_without_primary() -> None:
    from qq_ai_bot.domain.conversations import ConversationScope
    from qq_ai_bot.services.turn_coordinator import ConversationTurnCoordinator

    primary = ConversationScope.private("8000", "1001").key
    hydrated = InboundMessage(
        message_id="facade-v2",
        event_type="message:test",
        scope_type=ScopeType.PRIVATE,
        sender=SenderIdentity(user_id="1001"),
        text="hi",
        bot_user_id="8001",
        legacy_conversation_key=primary,
        conversation_id="conv-1",
    )
    invocation = PluginInvocation(
        plugin_id="demo.plugin",
        origin=TurnOrigin.USER_MESSAGE,
        actor_user_id="1001",
        bot_user_id="8001",
        inbound=hydrated,
    )
    assert invocation.conversation_key == primary
    assert ConversationTurnCoordinator.key_for(hydrated) == primary

    missing_primary = InboundMessage(
        message_id="facade-missing",
        event_type="message:test",
        scope_type=ScopeType.PRIVATE,
        sender=SenderIdentity(user_id="1001"),
        text="hi",
        bot_user_id="8001",
        conversation_id="conv-1",
    )
    closed = PluginInvocation(
        plugin_id="demo.plugin",
        origin=TurnOrigin.USER_MESSAGE,
        actor_user_id="1001",
        bot_user_id="8001",
        inbound=missing_primary,
        conversation_id="conv-1",
    )
    with pytest.raises(ValueError, match="missing primary runtime key"):
        _ = closed.conversation_key
    with pytest.raises(ValueError, match="missing primary runtime key"):
        ConversationTurnCoordinator.key_for(missing_primary)

    scheduled = PluginInvocation(
        plugin_id="demo.plugin",
        origin=TurnOrigin.USER_MESSAGE,
        actor_user_id="1001",
        bot_user_id="8001",
        conversation_id="conv-1",
    )
    with pytest.raises(ValueError, match="missing primary runtime key"):
        _ = scheduled.conversation_key

    v1_inbound = InboundMessage(
        message_id="facade-v1",
        event_type="message:test",
        scope_type=ScopeType.PRIVATE,
        sender=SenderIdentity(user_id="1001"),
        text="hi",
        bot_user_id="8001",
    )
    v1_invocation = PluginInvocation(
        plugin_id="demo.plugin",
        origin=TurnOrigin.USER_MESSAGE,
        actor_user_id="1001",
        bot_user_id="8001",
        inbound=v1_inbound,
    )
    assert v1_invocation.conversation_key == ConversationScope.private("8001", "1001").key
    assert ConversationTurnCoordinator.key_for(v1_inbound) == v1_invocation.conversation_key


@pytest.mark.asyncio
async def test_plugin_host_state_does_not_split_on_subject(database: Database) -> None:
    from qq_ai_bot.plugin_host.repository import PluginInstallationRepository

    await PluginInstallationRepository(database).upsert_discovered(
        plugin_id="demo.plugin",
        name="Demo",
        version="1.0.0",
        plugin_api="2.0",
        yuki_requires=">=3.0.0,<4.0",
        manifest_hash=("ab" * 32),
        entrypoint="plugin:Demo",
        requested_permissions=("storage.private",),
    )
    states = PluginStateRepository(database)
    first = await states.compare_and_set(
        plugin_id="demo.plugin",
        namespace="notes",
        key="sticky",
        expected_version=0,
        value={"text": "hello"},
    )
    second = await states.compare_and_set(
        plugin_id="demo.plugin",
        namespace="notes",
        key="sticky",
        expected_version=first.version,
        value={"text": "hello-2"},
    )
    assert second.version == first.version + 1


@pytest.mark.asyncio
async def test_automation_send_uses_current_binding_and_presence_provenance(
    database: Database,
) -> None:
    from qq_ai_bot.automation.gateway import OneBotProactiveGateway, ProactiveGatewayError
    from qq_ai_bot.identity.db_models import IdentityBindingModel
    from qq_ai_bot.identity.ingress import _ensure_person_id

    configure_identity_write_settings(IdentityWriteSettings(superusers=frozenset({"9000"})))
    await _flip_v2(database)
    registry = napcat_registry(gateway_instance_id="gw-auto")
    router = PresenceRouter(database, registry, membership_probe=_true)

    class _RecordBot:
        def __init__(self, self_id: str) -> None:
            self.self_id = self_id
            self.calls: list[tuple[str, dict[str, object]]] = []

        async def call_api(self, action: str, **kwargs: object) -> dict[str, object]:
            self.calls.append((action, dict(kwargs)))
            return {"message_id": f"{self.self_id}-out"}

    bot_a = _RecordBot("8000")
    bot_b = _RecordBot("8001")
    async with database.sessions() as session, session.begin():
        presence_a = await ensure_v2_presence(session, "8000")
        person_id = await _ensure_person_id(session, "1001")
    registry.connect(bot_a)
    registry.bind_presence(platform="qq", external_account_id="8000", presence_id=presence_a)
    await router.cas_takeover_person(person_id)
    ledger: list[dict[str, object]] = []

    class _Ledger:
        async def append(self, **kwargs: object) -> None:
            ledger.append(dict(kwargs))

    class _Actions:
        async def record(self, **_kwargs: object) -> None:
            return None

    gateway = OneBotProactiveGateway(
        bot_user_id="8000",
        creator_user_id="1001",
        automation_id=1,
        automation_run_id=1,
        ledger=_Ledger(),  # type: ignore[arg-type]
        actions=_Actions(),  # type: ignore[arg-type]
        registry=registry,
        router=router,
        target_person_id=person_id,
    )
    await gateway.send_private("1001", "hello")
    assert bot_a.calls == [("send_private_msg", {"user_id": "1001", "message": "hello"})]
    assert ledger[-1]["bot_user_id"] == "8000"
    assert ledger[-1]["private_peer_user_id"] == "1001"
    with pytest.raises(ProactiveGatewayError) as mismatch:
        await gateway.send_group("2001", "nope")
    assert mismatch.value.category == "capability"
    registry.disconnect(bot_a)
    async with database.sessions() as session, session.begin():
        presence_b = await ensure_v2_presence(session, "8001")
        binding = await session.scalar(
            select(IdentityBindingModel).where(IdentityBindingModel.person_id == person_id)
        )
        assert binding is not None
        binding.external_account_id = "1009"
    registry.connect(bot_b)
    registry.bind_presence(platform="qq", external_account_id="8001", presence_id=presence_b)
    taken = await router.cas_takeover_person(person_id)
    assert taken == "taken"
    await gateway.send_private("1001", "follow")
    assert bot_b.calls == [("send_private_msg", {"user_id": "1009", "message": "follow"})]
    assert ledger[-1]["bot_user_id"] == "8001"
    assert ledger[-1]["private_peer_user_id"] == "1009"
    registry.connect(bot_a)
    registry.bind_presence(platform="qq", external_account_id="8000", presence_id=presence_a)
    kept = await router.cas_takeover_person(person_id)
    assert kept == "unchanged"
    await gateway.send_private("1001", "still")
    assert bot_b.calls[-1] == ("send_private_msg", {"user_id": "1009", "message": "still"})
    assert ledger[-1]["bot_user_id"] == "8001"


@pytest.mark.asyncio
async def test_plugin_transport_uses_resolved_target_and_presence(
    database: Database,
) -> None:
    from qq_ai_bot.identity.db_models import IdentityBindingModel
    from qq_ai_bot.identity.ingress import _ensure_person_id
    from qq_ai_bot.plugin_host.notification_delivery import (
        NotificationDeliveryReceipt,
        OneBotNotificationTransport,
    )

    configure_identity_write_settings(IdentityWriteSettings(superusers=frozenset({"9000"})))
    await _flip_v2(database)
    registry = napcat_registry(gateway_instance_id="gw-plugin")
    router = PresenceRouter(database, registry, membership_probe=_true)

    class _RecordBot:
        def __init__(self, self_id: str) -> None:
            self.self_id = self_id
            self.calls: list[tuple[str, dict[str, object]]] = []

        async def call_api(self, action: str, **kwargs: object) -> dict[str, object]:
            self.calls.append((action, dict(kwargs)))
            return {"message_id": "plugin-1"}

    bot = _RecordBot("8001")
    async with database.sessions() as session, session.begin():
        presence = await ensure_v2_presence(session, "8001")
        person_id = await _ensure_person_id(session, "1001")
        binding = await session.scalar(
            select(IdentityBindingModel).where(IdentityBindingModel.person_id == person_id)
        )
        assert binding is not None
        binding.external_account_id = "1009"
    registry.connect(bot)
    registry.bind_presence(platform="qq", external_account_id="8001", presence_id=presence)
    await router.cas_takeover_person(person_id)
    transport = OneBotNotificationTransport(registry, router=router)
    receipt = await transport.send_text(
        bot_user_id="8000",
        target_type="private",
        target_id="1001",
        text="ping",
        canonical_target_person_id=person_id,
    )
    assert isinstance(receipt, NotificationDeliveryReceipt)
    assert receipt.message_id == "plugin-1"
    assert bot.calls == [("send_private_msg", {"user_id": "1009", "message": "ping"})]
    assert receipt.sender_account_id == "8001"
    assert receipt.external_target_id == "1009"
    assert receipt.route_kind == "person"
    assert not hasattr(transport, "last_sender_account_id")
    assert not hasattr(transport, "last_external_target_id")
    from qq_ai_bot.automation.gateway import ProactiveGatewayError

    with pytest.raises(ProactiveGatewayError) as missing:
        await transport.send_text(
            bot_user_id="8001",
            target_type="private",
            target_id="404",
            text="missing",
        )
    assert missing.value.category == "none"


@pytest.mark.asyncio
async def test_v1_bootstrap_does_not_insert_legacy_identity_rows(database: Database) -> None:
    from types import SimpleNamespace

    from qq_ai_bot.conversation.rollup.db_models import ConversationScopeModel
    from qq_ai_bot.identity.bootstrap import bootstrap_settings_identity
    from qq_ai_bot.identity.db_models import IdentityBindingModel, SpaceBindingModel
    from qq_ai_bot.persistence.models import GroupModel, PersonModel

    settings = SimpleNamespace(superusers=frozenset({"1001"}), enabled_groups=frozenset({"2001"}))
    await bootstrap_settings_identity(database, settings)  # type: ignore[arg-type]
    async with database.sessions() as session:
        people = int(await session.scalar(select(func.count()).select_from(PersonModel)) or 0)
        groups = int(await session.scalar(select(func.count()).select_from(GroupModel)) or 0)
        scopes = int(
            await session.scalar(select(func.count()).select_from(ConversationScopeModel)) or 0
        )
        bindings = int(
            await session.scalar(select(func.count()).select_from(IdentityBindingModel)) or 0
        )
        spaces = int(await session.scalar(select(func.count()).select_from(SpaceBindingModel)) or 0)
    assert people == 0
    assert groups == 0
    assert scopes == 0
    assert bindings == 1
    assert spaces == 1


@pytest.mark.asyncio
async def test_v2_bootstrap_does_not_insert_legacy_identity_rows(database: Database) -> None:
    from types import SimpleNamespace

    from qq_ai_bot.conversation.rollup.db_models import ConversationScopeModel
    from qq_ai_bot.identity.bootstrap import bootstrap_settings_identity
    from qq_ai_bot.identity.db_models import IdentityBindingModel, SpaceBindingModel
    from qq_ai_bot.persistence.models import GroupModel, PersonModel

    await _flip_v2(database)
    settings = SimpleNamespace(superusers=frozenset({"1001"}), enabled_groups=frozenset({"2001"}))
    await bootstrap_settings_identity(database, settings)  # type: ignore[arg-type]
    async with database.sessions() as session:
        people = int(await session.scalar(select(func.count()).select_from(PersonModel)) or 0)
        groups = int(await session.scalar(select(func.count()).select_from(GroupModel)) or 0)
        scopes = int(
            await session.scalar(select(func.count()).select_from(ConversationScopeModel)) or 0
        )
        bindings = int(
            await session.scalar(select(func.count()).select_from(IdentityBindingModel)) or 0
        )
        spaces = int(await session.scalar(select(func.count()).select_from(SpaceBindingModel)) or 0)
    assert people == 0
    assert groups == 0
    assert scopes == 0
    assert bindings == 1
    assert spaces == 1


@pytest.mark.asyncio
async def test_v2_live_memory_refuses_null_canonical_event(database: Database) -> None:
    from qq_ai_bot.identity.ingress import _ensure_person_id
    from qq_ai_bot.identity.memory_guard import refuse_legacy_live_event, refuse_legacy_live_fact
    from qq_ai_bot.memory.enums import MemoryJobStatus
    from qq_ai_bot.memory.reflection.repository import MemoryReflectionRepository
    from qq_ai_bot.memory.self_reflection.repository import SelfReflectionRepository
    from qq_ai_bot.persistence.models import ChatEventModel, MemoryEvidenceModel, MemoryJobModel

    configure_identity_write_settings(IdentityWriteSettings(superusers=frozenset({"9000"})))
    await _flip_v2(database)
    jobs = MemoryJobRepository(database)
    facts = MemoryFactRepository(database)
    reflection = MemoryReflectionRepository(database)
    self_reflection = SelfReflectionRepository(database)
    await self_reflection.scan_new_events()
    async with database.sessions() as session, session.begin():
        person_id = await _ensure_person_id(session, "1001")
        event = ChatEventModel(
            bot_user_id="8000",
            platform_message_id="legacy-1",
            scope_type="private",
            private_peer_user_id="1001",
            sender_user_id="1001",
            sender_nickname="",
            sender_group_card="",
            direction="inbound",
            event_kind="message",
            content="old",
            visual_summary="",
            segments_json="[]",
            origin="user_message",
            occurred_at=_NOW,
            observed_at=_NOW,
        )
        session.add(event)
        await session.flush()
        event_id = event.id
        assert event.canonical_event_id is None
        assert await refuse_legacy_live_event(session, event)
        fact = await facts.create_fact(
            MemoryFactCreate(
                scope_type=MemoryScopeType.PERSON,
                subject_user_id="1001",
                kind=MemoryKind.FACT,
                memory_key="legacy.color",
                category="preference",
                content="red",
                source_type=MemorySourceType.EXPLICIT,
            ),
            normalized_content="red",
            supersedes_id=None,
            session=session,
        )
        session.add(
            MemoryEvidenceModel(
                fact_id=fact.id,
                event_id=event_id,
                source_speaker_user_id="1001",
                relation="self_statement",
                confidence=1.0,
                authority="self_report",
                excerpt="old",
                created_at=_NOW,
            )
        )
        stored = await session.get(MemoryFactModel, fact.id)
        assert stored is not None
        assert stored.canonical_subject_person_id == person_id
        fact_id = fact.id
    assert await jobs.enqueue(event_id, "private:1001") is False
    async with database.sessions() as session, session.begin():
        session.add(
            MemoryJobModel(
                event_id=event_id,
                conversation_key="private:1001",
                status=MemoryJobStatus.PENDING.value,
                attempts=0,
                next_attempt_at=_NOW,
                created_at=_NOW,
                updated_at=_NOW,
            )
        )
    claimed = await jobs.claim(limit=10)
    assert claimed == ()
    async with database.sessions() as session:
        leftover = await session.scalar(
            select(MemoryJobModel).where(MemoryJobModel.event_id == event_id)
        )
        assert leftover is not None
        assert leftover.status == MemoryJobStatus.FAILED.value
        assert leftover.error_category == "legacy_event_replay"
        assert await refuse_legacy_live_fact(session, fact_id)
    scanned = await self_reflection.scan_new_events()
    assert scanned >= 1
    async with database.sessions() as session:
        from qq_ai_bot.persistence.models import MemorySelfReflectionStateModel

        pending = int(
            await session.scalar(
                select(func.coalesce(func.sum(MemorySelfReflectionStateModel.pending_events), 0))
            )
            or 0
        )
    assert pending == 0
    discovered = await reflection.discover(limit=20)
    assert all(item.fact_id != fact_id for item in discovered)


def _chat_event(
    *,
    platform_message_id: str,
    content: str,
    canonical_event_id: str | None = None,
    canonical_conversation_id: str | None = None,
    event_id: int | None = None,
) -> ChatEventModel:
    row = ChatEventModel(
        bot_user_id="8000",
        platform_message_id=platform_message_id,
        scope_type="private",
        private_peer_user_id="1001",
        sender_user_id="1001",
        sender_nickname="",
        sender_group_card="",
        direction="inbound",
        event_kind="message",
        content=content,
        visual_summary="",
        segments_json="[]",
        origin="user_message",
        occurred_at=_NOW,
        observed_at=_NOW,
        canonical_event_id=canonical_event_id,
        canonical_conversation_id=canonical_conversation_id,
    )
    if event_id is not None:
        row.id = event_id
    return row


async def _seed_guard_conversation(
    session: object,
    *,
    starts_after_event_id: int,
    last_event_id: int,
    last_generation_change_event_id: int,
    covered_through_event_id: int,
    generation: int = 1,
) -> str:
    from qq_ai_bot.conversation.canonical_db_models import (
        CanonicalConversationModel,
        ConversationLegacyAliasModel,
    )
    from qq_ai_bot.identity.ingress import _ensure_person_id
    from qq_ai_bot.persistence.models import PersonModel

    person_id = await _ensure_person_id(session, "1001")  # type: ignore[arg-type]
    if await session.get(PersonModel, "1001") is None:  # type: ignore[union-attr]
        session.add(  # type: ignore[union-attr]
            PersonModel(
                user_id="1001",
                nickname="",
                enabled=True,
                is_bot=False,
                first_seen_at=_NOW,
                last_seen_at=_NOW,
            )
        )
    conversation_id = str(uuid4())
    alias_id = str(uuid4())
    session.add(  # type: ignore[union-attr]
        CanonicalConversationModel(
            id=conversation_id,
            kind="private",
            person_id=person_id,
            space_id=None,
            primary_alias_id=alias_id,
            primary_marker=1,
            generation=generation,
            starts_after_event_id=starts_after_event_id,
            last_event_id=last_event_id,
            last_generation_change_event_id=last_generation_change_event_id,
            covered_through_event_id=covered_through_event_id,
            uncovered_event_count=0,
            uncovered_character_count=0,
            revision=1,
            created_at=_NOW,
            updated_at=_NOW,
        )
    )
    session.add(  # type: ignore[union-attr]
        ConversationLegacyAliasModel(
            id=alias_id,
            conversation_id=conversation_id,
            scope_key=f"bot:8000:private:1001:{alias_id}",
            is_primary=1,
            created_at=_NOW,
            updated_at=_NOW,
        )
    )
    return conversation_id


@pytest.mark.asyncio
async def test_v2_refuse_legacy_live_event_uses_conversation_watermark(
    database: Database,
) -> None:
    from qq_ai_bot.identity.memory_guard import refuse_legacy_live_event

    configure_identity_write_settings(IdentityWriteSettings(superusers=frozenset({"9000"})))
    await _flip_v2(database)
    async with database.sessions() as session, session.begin():
        conversation_id = await _seed_guard_conversation(
            session,
            starts_after_event_id=5,
            last_event_id=10,
            last_generation_change_event_id=5,
            covered_through_event_id=5,
        )
        old = _chat_event(
            platform_message_id="cutover-old",
            content="old mapped",
            canonical_event_id=str(uuid4()),
            canonical_conversation_id=conversation_id,
            event_id=5,
        )
        fresh = _chat_event(
            platform_message_id="cutover-new",
            content="post cutover",
            canonical_event_id=str(uuid4()),
            canonical_conversation_id=conversation_id,
            event_id=10,
        )
        unmapped = _chat_event(
            platform_message_id="cutover-unmapped",
            content="unmapped",
            canonical_event_id=str(uuid4()),
        )
        missing = _chat_event(
            platform_message_id="cutover-missing-event",
            content="missing id",
        )
        session.add_all([old, fresh, unmapped, missing])
        await session.flush()
        assert await refuse_legacy_live_event(session, old)
        assert not await refuse_legacy_live_event(session, fresh)
        assert await refuse_legacy_live_event(session, unmapped)
        assert await refuse_legacy_live_event(session, missing)
        old_pk = old.id
        fresh_pk = fresh.id
        unmapped_pk = unmapped.id
        missing_pk = missing.id
    jobs = MemoryJobRepository(database)
    assert await jobs.enqueue(old_pk, "private:1001") is False
    assert await jobs.enqueue(fresh_pk, "private:1001") is True
    assert await jobs.enqueue(unmapped_pk, "private:1001") is False
    assert await jobs.enqueue(missing_pk, "private:1001") is False


@pytest.mark.asyncio
async def test_v2_refuse_legacy_live_event_generation_mismatch(database: Database) -> None:
    from qq_ai_bot.identity.memory_guard import refuse_legacy_live_event

    configure_identity_write_settings(IdentityWriteSettings(superusers=frozenset({"9000"})))
    await _flip_v2(database)
    async with database.sessions() as session, session.begin():
        conversation_id = await _seed_guard_conversation(
            session,
            starts_after_event_id=0,
            last_event_id=8,
            last_generation_change_event_id=8,
            covered_through_event_id=8,
            generation=2,
        )
        event = _chat_event(
            platform_message_id="gen-mismatch",
            content="before generation",
            canonical_event_id=str(uuid4()),
            canonical_conversation_id=conversation_id,
            event_id=8,
        )
        session.add(event)
        await session.flush()
        assert await refuse_legacy_live_event(session, event)


@pytest.mark.asyncio
async def test_created_automation_sends_persisted_person_not_creator(
    database: Database,
) -> None:
    from tests.conftest import make_settings

    from qq_ai_bot.automation.gateway import OneBotProactiveGateway, ProactiveGatewayError
    from qq_ai_bot.automation.models import AutomationScript
    from qq_ai_bot.automation.registry import build_capability_registry
    from qq_ai_bot.automation.repository import AutomationRepository
    from qq_ai_bot.automation.service import AutomationService
    from qq_ai_bot.identity.db_models import IdentityBindingModel
    from qq_ai_bot.identity.ingress import _ensure_person_id
    from qq_ai_bot.time.service import TimeContextService

    settings = make_settings(database.url, automation_enabled=True, superusers_csv="9000")
    service = AutomationService(
        settings=settings,
        repository=AutomationRepository(database),
        registry=build_capability_registry(),
        time_service=TimeContextService(database),
    )
    script = AutomationScript.model_validate(
        {
            "version": 1,
            "name": "定向",
            "timezone": "Asia/Shanghai",
            "schedule": {"type": "after", "seconds": 1},
            "context": {"scene": "none"},
            "steps": [
                {
                    "id": "send",
                    "call": "onebot.send_private_message",
                    "arguments": {"user_id": "1808058482", "text": "给别人"},
                }
            ],
            "limits": {
                "max_steps": 1,
                "max_llm_calls": 0,
                "max_tool_calls": 1,
                "max_messages": 1,
                "timeout_seconds": 30,
            },
        }
    )
    inbound = InboundMessage(
        message_id="auto-explicit",
        event_type="private",
        scope_type=ScopeType.PRIVATE,
        sender=SenderIdentity(user_id="9000", nickname="超管"),
        text="1秒后提醒 1808058482",
        raw_text="1秒后提醒 1808058482",
        bot_user_id="8001",
    )
    row = await service.create(script, inbound=inbound, conversation_key="private:9000")
    assert row.canonical_target_person_id is not None
    assert row.canonical_target_person_id != row.canonical_creator_person_id
    configure_identity_write_settings(IdentityWriteSettings(superusers=frozenset({"9000"})))
    await _flip_v2(database)
    registry = napcat_registry(gateway_instance_id="gw-auto-persist")
    router = PresenceRouter(database, registry, membership_probe=_true)

    class _RecordBot:
        def __init__(self, self_id: str) -> None:
            self.self_id = self_id
            self.calls: list[tuple[str, dict[str, object]]] = []

        async def call_api(self, action: str, **kwargs: object) -> dict[str, object]:
            self.calls.append((action, dict(kwargs)))
            return {"message_id": "auto-persist"}

    bot = _RecordBot("8001")
    async with database.sessions() as session, session.begin():
        presence = await ensure_v2_presence(session, "8001")
        await _ensure_person_id(session, "9000")
        target_person = row.canonical_target_person_id
        binding = await session.scalar(
            select(IdentityBindingModel).where(IdentityBindingModel.person_id == target_person)
        )
        assert binding is not None
        binding.external_account_id = "1009"
    registry.connect(bot)
    registry.bind_presence(platform="qq", external_account_id="8001", presence_id=presence)
    assert await router.cas_takeover_person(row.canonical_target_person_id) == "taken"
    ledger: list[dict[str, object]] = []

    class _Ledger:
        async def append(self, **kwargs: object) -> None:
            ledger.append(dict(kwargs))

    class _Actions:
        async def record(self, **_kwargs: object) -> None:
            return None

    gateway = OneBotProactiveGateway(
        bot_user_id="8001",
        creator_user_id="9000",
        automation_id=row.id,
        automation_run_id=1,
        ledger=_Ledger(),  # type: ignore[arg-type]
        actions=_Actions(),  # type: ignore[arg-type]
        registry=registry,
        router=router,
        target_person_id=row.canonical_target_person_id,
    )
    await gateway.send_private("1808058482", "给别人")
    assert bot.calls == [("send_private_msg", {"user_id": "1009", "message": "给别人"})]
    assert ledger[-1]["private_peer_user_id"] == "1009"
    with pytest.raises(ProactiveGatewayError) as foreign:
        await gateway.send_private("9000", "错投")
    assert foreign.value.category == "target_mismatch"
    assert bot.calls == [("send_private_msg", {"user_id": "1009", "message": "给别人"})]


def test_scope_inventory_has_owner_for_every_fillable_shadow() -> None:
    assert any(item[0] == "people.canonical_person_id" for item in FILLABLE_SHADOWS)
    assert any(item[0] == "automations.canonical_target_person_id" for item in FILLABLE_SHADOWS)
    assert any(
        item[0] == "runtime_turn_observations.canonical_person_id" for item in FILLABLE_SHADOWS
    )
    deferred = {item[0] for item in DEFERRED_SHADOWS}
    baseline = {item[0] for item in CUTOVER_BASELINE_PENDING}
    provenance = {item[0] for item in LEGACY_PROVENANCE_RETAINED}
    deferred_text = " ".join(deferred)
    assert "canonical_conversation_id" in deferred_text
    assert "automations.canonical_target_person_id/space_id" not in deferred_text
    assert "memory_jobs" not in deferred
    assert "memory_evidence" not in deferred
    assert "memory_tool_receipts" not in deferred
    assert "memory_reflection_jobs" not in deferred
    assert "memory_self_reflection_states/memory_self_reflection_runs" not in deferred
    assert "memory_dream_runs/memory_dream_clusters/memory_dream_operations" not in deferred
    assert "memory_jobs" in baseline
    assert "memory_evidence" in baseline
    assert "memory_reflection_jobs" in baseline
    assert "memory_tool_receipts.bot_user_id/conversation_key_hash" in provenance
    assert "memory_evidence.source_speaker_user_id" in provenance


@pytest.mark.asyncio
async def test_delete_person_removes_canonical_private_and_group_overlays(
    database: Database,
) -> None:
    from qq_ai_bot.conversation.canonical_db_models import (
        CanonicalConversationRollupEmergencyOverlayModel,
    )
    from qq_ai_bot.conversation.rollup.models import RollupPolicyConfig
    from qq_ai_bot.conversation.rollup.repository import ConversationRollupRepository
    from qq_ai_bot.conversation.rollup.service import ConversationRollupService
    from qq_ai_bot.domain.conversations import ConversationScope
    from qq_ai_bot.identity.dual_write import (
        ensure_canonical_person_preconfig,
        ensure_canonical_presence_preconfig,
        ensure_canonical_space_preconfig,
    )
    from qq_ai_bot.persistence.repositories import PeopleRepository
    from qq_ai_bot.persistence.scoped_event_uow import ScopedEventLedgerUnitOfWork

    await _flip_v2(database)
    async with database.sessions() as session, session.begin():
        from qq_ai_bot.persistence.models import PersonModel

        await ensure_canonical_presence_preconfig(session, "8000")
        person_id = await ensure_canonical_person_preconfig(session, "1001", now=_NOW)
        await ensure_canonical_space_preconfig(session, "2001", now=_NOW)
        session.add(
            PersonModel(
                user_id="1001",
                nickname="Ada",
                enabled=True,
                is_bot=False,
                first_seen_at=_NOW,
                last_seen_at=_NOW,
                canonical_person_id=person_id,
            )
        )
    policy = RollupPolicyConfig(
        raw_tail_events=2,
        raw_tail_characters=100_000,
        trigger_events=2,
        trigger_characters=100_000,
        stop_events=0,
        stop_characters=0,
        batch_max_events=100,
        batch_max_characters=100_000,
        summary_max_characters=2_000,
    )
    uow = ScopedEventLedgerUnitOfWork(database, config=policy)
    repository = ConversationRollupRepository(database, policy)
    service = ConversationRollupService(models=None, config=policy, timeout_seconds=0.1)
    private = ConversationScope.private("8000", "1001")
    group = ConversationScope.group("8000", "2001")
    for scope, prefix in ((private, "cpriv"), (group, "cgrp")):
        for index in range(1, 5):
            await uow.append(
                scope=scope,
                platform_message_id=f"{prefix}-{index}",
                sender_user_id="1001",
                direction="inbound",
                content=f"canonical-secret-{index}",
                occurred_at=_NOW.replace(second=index),
            )
        claim = await repository.claim_scope_for_foreground(
            scope, lease_owner=prefix, lease_seconds=30
        )
        assert claim is not None
        candidate = await repository.candidate_for_claim(claim, emergency=True)
        assert candidate is not None
        summary, _kind = service.emergency(candidate)
        await repository.commit_emergency_overlay(claim, candidate, summary)
    async with database.sessions() as session:
        leftover = int(
            await session.scalar(
                select(func.count(CanonicalConversationRollupEmergencyOverlayModel.conversation_id))
            )
            or 0
        )
    assert leftover == 2
    assert await PeopleRepository(database).delete_person("1001") is True
    async with database.sessions() as session:
        leftover = int(
            await session.scalar(
                select(func.count(CanonicalConversationRollupEmergencyOverlayModel.conversation_id))
            )
            or 0
        )
    assert leftover == 0


async def _v2_private_ledger(database: Database):
    from qq_ai_bot.conversation.rollup.models import RollupPolicyConfig
    from qq_ai_bot.domain.conversations import ConversationScope
    from qq_ai_bot.identity.dual_write import (
        ensure_canonical_person_preconfig,
        ensure_canonical_presence_preconfig,
    )
    from qq_ai_bot.persistence.scoped_event_uow import ScopedEventLedgerUnitOfWork

    await _flip_v2(database)
    async with database.sessions() as session, session.begin():
        await ensure_canonical_presence_preconfig(session, "8000")
        await ensure_canonical_person_preconfig(session, "1001", now=_NOW)
    return (
        ScopedEventLedgerUnitOfWork(database, config=RollupPolicyConfig()),
        ConversationScope.private("8000", "1001"),
    )


async def _v2_ledger_snapshot(database: Database) -> tuple[object, object, object]:
    from qq_ai_bot.conversation.canonical_db_models import CanonicalEventReceiptModel
    from qq_ai_bot.persistence.models import MemoryJobModel

    async with database.sessions() as session:
        events = [
            (
                row.id,
                row.canonical_event_id,
                row.content,
                row.segments_json,
                row.occurred_at,
                row.suppression_status,
            )
            for row in (await session.scalars(select(ChatEventModel))).all()
        ]
        receipts = [
            (row.id, row.canonical_event_id, row.platform_message_id, row.event_type)
            for row in (await session.scalars(select(CanonicalEventReceiptModel))).all()
        ]
        jobs = [
            (row.id, row.event_id, row.status)
            for row in (await session.scalars(select(MemoryJobModel))).all()
        ]
    return (events, receipts, jobs)


@pytest.mark.asyncio
async def test_v2_live_identical_repeat_reuses_receipt_and_ledger(database: Database) -> None:
    from qq_ai_bot.conversation.canonical_db_models import CanonicalEventReceiptModel
    from qq_ai_bot.memory.repository import MemoryJobRepository
    from qq_ai_bot.persistence.models import MemoryJobModel, PersonModel

    uow, scope = await _v2_private_ledger(database)
    first = await uow.append(
        scope=scope,
        platform_message_id="live-dup-1",
        sender_user_id="1001",
        direction="inbound",
        content="same-payload",
        segments=({"type": "text", "data": {"text": "same-payload", "k": "v"}},),
        occurred_at=_NOW,
    )
    assert first.created is True
    async with database.sessions() as session, session.begin():
        row = await session.get(ChatEventModel, first.event.id)
        assert row is not None and row.canonical_event_id
        session.add(
            PersonModel(
                user_id="1001",
                nickname="Ada",
                enabled=True,
                is_bot=False,
                first_seen_at=_NOW,
                last_seen_at=_NOW,
                canonical_person_id=row.author_person_id,
            )
        )
        canonical_event_id = row.canonical_event_id
    enqueued = await MemoryJobRepository(database).enqueue(first.event.id, "private:1001")
    assert enqueued is True
    before = await _v2_ledger_snapshot(database)
    replayed = await uow.append(
        scope=scope,
        platform_message_id="live-dup-1",
        sender_user_id="1001",
        direction="inbound",
        content="same-payload",
        segments=({"data": {"k": "v", "text": "same-payload"}, "type": "text"},),
        occurred_at=_NOW,
    )
    assert replayed.created is False
    assert replayed.job_signalled is False
    assert replayed.event.id == first.event.id
    async with database.sessions() as session:
        row = await session.get(ChatEventModel, first.event.id)
        receipts = list(await session.scalars(select(CanonicalEventReceiptModel)))
        jobs = list(await session.scalars(select(MemoryJobModel)))
        events = list(await session.scalars(select(ChatEventModel)))
        assert row is not None
        assert row.canonical_event_id == canonical_event_id
        assert len(events) == 1
        assert len(receipts) == 1
        assert receipts[0].canonical_event_id == canonical_event_id
        assert len(jobs) == 1
    assert await _v2_ledger_snapshot(database) == before


@pytest.mark.asyncio
async def test_v2_live_receipt_content_conflict_keeps_original(database: Database) -> None:
    from qq_ai_bot.identity.errors import IdentityDualWriteError

    uow, scope = await _v2_private_ledger(database)
    first = await uow.append(
        scope=scope,
        platform_message_id="live-conflict-1",
        sender_user_id="1001",
        direction="inbound",
        content="original-body",
        occurred_at=_NOW,
    )
    before = await _v2_ledger_snapshot(database)
    with pytest.raises(IdentityDualWriteError) as exc:
        await uow.append(
            scope=scope,
            platform_message_id="live-conflict-1",
            sender_user_id="1001",
            direction="inbound",
            content="tampered-body",
            occurred_at=_NOW,
        )
    assert exc.value.category == "receipt_conflict"
    assert await _v2_ledger_snapshot(database) == before
    async with database.sessions() as session:
        row = await session.get(ChatEventModel, first.event.id)
        assert row is not None
        assert row.content == "original-body"


@pytest.mark.asyncio
async def test_v2_live_segments_or_occurred_at_conflict_fails_closed(database: Database) -> None:
    from qq_ai_bot.identity.errors import IdentityDualWriteError

    uow, scope = await _v2_private_ledger(database)
    first = await uow.append(
        scope=scope,
        platform_message_id="live-payload-1",
        sender_user_id="1001",
        direction="inbound",
        content="payload",
        segments=({"type": "text", "data": {"text": "one"}},),
        occurred_at=_NOW,
    )
    before = await _v2_ledger_snapshot(database)
    with pytest.raises(IdentityDualWriteError) as segments:
        await uow.append(
            scope=scope,
            platform_message_id="live-payload-1",
            sender_user_id="1001",
            direction="inbound",
            content="payload",
            segments=({"type": "text", "data": {"text": "two"}},),
            occurred_at=_NOW,
        )
    with pytest.raises(IdentityDualWriteError) as occurred:
        await uow.append(
            scope=scope,
            platform_message_id="live-payload-1",
            sender_user_id="1001",
            direction="inbound",
            content="payload",
            segments=({"type": "text", "data": {"text": "one"}},),
            occurred_at=_NOW.replace(minute=1),
        )
    assert segments.value.category == "receipt_conflict"
    assert occurred.value.category == "receipt_conflict"
    assert await _v2_ledger_snapshot(database) == before
    async with database.sessions() as session:
        row = await session.get(ChatEventModel, first.event.id)
        assert row is not None
        assert row.content == "payload"
        assert '"one"' in row.segments_json
        stored = row.occurred_at
        if stored.tzinfo is None:
            stored = stored.replace(tzinfo=UTC)
        assert stored == _NOW


@pytest.mark.asyncio
async def test_v2_live_receipt_race_rereads_winner_not_new_uuid(
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from uuid import uuid4

    from qq_ai_bot.conversation.canonical_db_models import CanonicalEventReceiptModel
    from qq_ai_bot.identity.errors import IdentityDualWriteError
    from qq_ai_bot.persistence.scoped_event_uow import ScopedEventLedgerUnitOfWork

    uow, scope = await _v2_private_ledger(database)
    first = await uow.append(
        scope=scope,
        platform_message_id="live-race-1",
        sender_user_id="1001",
        direction="inbound",
        content="race-winner",
        occurred_at=_NOW,
    )
    async with database.sessions() as session:
        winner = await session.get(ChatEventModel, first.event.id)
        assert winner is not None and winner.canonical_event_id
        missing = await session.scalar(
            select(ChatEventModel).where(ChatEventModel.canonical_event_id == str(uuid4()))
        )
        assert missing is None
        receipt, claimed = await uow._load_receipt_claimed_event(
            session,
            presence_id=winner.ingress_presence_id or "",
            event_type="message",
            platform_message_id="live-race-1",
        )
        assert receipt is not None
        assert claimed is not None
        assert claimed.id == first.event.id
        assert receipt.canonical_event_id == winner.canonical_event_id

    async def _miss(*_args: object, **_kwargs: object) -> tuple[None, None]:
        return None, None

    monkeypatch.setattr(ScopedEventLedgerUnitOfWork, "_find_existing_v2_live", _miss)
    replayed = await uow.append(
        scope=scope,
        platform_message_id="live-race-1",
        sender_user_id="1001",
        direction="inbound",
        content="race-winner",
        occurred_at=_NOW,
    )
    assert replayed.created is False
    assert replayed.event.id == first.event.id
    before_conflict = await _v2_ledger_snapshot(database)
    with pytest.raises(IdentityDualWriteError) as exc:
        await uow.append(
            scope=scope,
            platform_message_id="live-race-1",
            sender_user_id="1001",
            direction="inbound",
            content="race-loser",
            occurred_at=_NOW,
        )
    assert exc.value.category == "receipt_conflict"
    assert await _v2_ledger_snapshot(database) == before_conflict
    async with database.sessions() as session:
        receipts = list(await session.scalars(select(CanonicalEventReceiptModel)))
        events = list(await session.scalars(select(ChatEventModel)))
        assert len(receipts) == 1
        assert len(events) == 1


@pytest.mark.asyncio
async def test_v2_plugin_external_stays_off_receipts_and_keeps_unique_key(
    database: Database,
) -> None:
    from qq_ai_bot.conversation.canonical_db_models import CanonicalEventReceiptModel
    from qq_ai_bot.identity.errors import IdentityDualWriteError

    uow, scope = await _v2_private_ledger(database)
    first = await uow.append_external(
        scope=scope,
        platform_message_id="plugin-ext-1",
        source_plugin_id="ext-plugin",
        external_source="github",
        external_event_key="push-unique",
        external_event_type="PushEvent",
        external_payload={"ok": True},
        external_target_id="1001",
        content="push",
        occurred_at=_NOW,
    )
    assert first.created is True
    before = await _v2_ledger_snapshot(database)
    reused = await uow.append_external(
        scope=scope,
        platform_message_id="plugin-ext-1",
        source_plugin_id="ext-plugin",
        external_source="github",
        external_event_key="push-unique",
        external_event_type="PushEvent",
        external_payload={"ok": True},
        external_target_id="1001",
        content="push",
        occurred_at=_NOW,
    )
    assert reused.created is False
    assert reused.event.id == first.event.id
    assert await _v2_ledger_snapshot(database) == before
    async with database.sessions() as session:
        receipts = list(await session.scalars(select(CanonicalEventReceiptModel)))
        events = list(await session.scalars(select(ChatEventModel)))
        row = await session.get(ChatEventModel, first.event.id)
        assert receipts == []
        assert len(events) == 1
        assert row is not None
        assert row.event_kind == "external_event"
        assert row.content == "push"
        assert row.external_event_key == "push-unique"
        assert row.platform_message_id == "plugin-ext-1"

    with pytest.raises(IdentityDualWriteError) as platform_conflict:
        await uow.append_external(
            scope=scope,
            platform_message_id="plugin-ext-2",
            source_plugin_id="ext-plugin",
            external_source="github",
            external_event_key="push-unique",
            external_event_type="PushEvent",
            external_payload={"ok": True},
            external_target_id="1001",
            content="push",
            occurred_at=_NOW,
        )
    assert platform_conflict.value.category == "receipt_conflict"
    assert await _v2_ledger_snapshot(database) == before

    with pytest.raises(IdentityDualWriteError) as payload_conflict:
        await uow.append_external(
            scope=scope,
            platform_message_id="plugin-ext-1",
            source_plugin_id="ext-plugin",
            external_source="github",
            external_event_key="push-unique",
            external_event_type="PushEvent",
            external_payload={"ok": False},
            external_target_id="1001",
            content="push",
            occurred_at=_NOW,
        )
    assert payload_conflict.value.category == "receipt_conflict"
    assert await _v2_ledger_snapshot(database) == before

    with pytest.raises(IdentityDualWriteError) as content_conflict:
        await uow.append_external(
            scope=scope,
            platform_message_id="plugin-ext-1",
            source_plugin_id="ext-plugin",
            external_source="github",
            external_event_key="push-unique",
            external_event_type="PushEvent",
            external_payload={"ok": True},
            external_target_id="1001",
            content="other-push",
            occurred_at=_NOW,
        )
    assert content_conflict.value.category == "receipt_conflict"
    assert await _v2_ledger_snapshot(database) == before
    async with database.sessions() as session:
        receipts = list(await session.scalars(select(CanonicalEventReceiptModel)))
        events = list(await session.scalars(select(ChatEventModel)))
        assert receipts == []
        assert len(events) == 1
        assert events[0].id == first.event.id
        assert events[0].content == "push"


@pytest.mark.asyncio
async def test_v2_plugin_external_occurred_at_conflict_keeps_original(
    database: Database,
) -> None:
    from qq_ai_bot.conversation.canonical_db_models import CanonicalEventReceiptModel
    from qq_ai_bot.identity.errors import IdentityDualWriteError

    uow, scope = await _v2_private_ledger(database)
    first = await uow.append_external(
        scope=scope,
        platform_message_id="plugin-ext-time-1",
        source_plugin_id="ext-plugin",
        external_source="github",
        external_event_key="push-time",
        external_event_type="PushEvent",
        external_payload={"ok": True},
        external_target_id="1001",
        content="push-time",
        occurred_at=_NOW,
    )
    assert first.created is True
    before = await _v2_ledger_snapshot(database)
    with pytest.raises(IdentityDualWriteError) as exc:
        await uow.append_external(
            scope=scope,
            platform_message_id="plugin-ext-time-1",
            source_plugin_id="ext-plugin",
            external_source="github",
            external_event_key="push-time",
            external_event_type="PushEvent",
            external_payload={"ok": True},
            external_target_id="1001",
            content="push-time",
            occurred_at=_NOW.replace(minute=1),
        )
    assert exc.value.category == "receipt_conflict"
    assert str(exc.value) == "identity dual-write failed"
    assert "push-time" not in str(exc.value)
    assert await _v2_ledger_snapshot(database) == before
    async with database.sessions() as session:
        receipts = list(await session.scalars(select(CanonicalEventReceiptModel)))
        events = list(await session.scalars(select(ChatEventModel)))
        row = await session.get(ChatEventModel, first.event.id)
        assert receipts == []
        assert len(events) == 1
        assert events[0].id == first.event.id
        assert row is not None
        assert row.content == "push-time"
        stored = row.occurred_at
        if stored.tzinfo is None:
            stored = stored.replace(tzinfo=UTC)
        assert stored == _NOW


@pytest.mark.asyncio
async def test_v2_plugin_external_unique_index_race_rereads_winner(
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from qq_ai_bot.conversation.canonical_db_models import CanonicalEventReceiptModel
    from qq_ai_bot.persistence.models import MemoryJobModel
    from qq_ai_bot.persistence.scoped_event_uow import ScopedEventLedgerUnitOfWork

    uow, scope = await _v2_private_ledger(database)
    first = await uow.append_external(
        scope=scope,
        platform_message_id="plugin-ext-race-1",
        source_plugin_id="ext-plugin",
        external_source="github",
        external_event_key="push-race",
        external_event_type="PushEvent",
        external_payload={"ok": True},
        external_target_id="1001",
        content="race-winner",
        occurred_at=_NOW,
    )
    assert first.created is True
    before = await _v2_ledger_snapshot(database)
    original = ScopedEventLedgerUnitOfWork._find_existing_v2_plugin_external
    calls = {"n": 0}

    async def _miss_once(*args: object, **kwargs: object) -> object:
        calls["n"] += 1
        if calls["n"] == 1:
            return None
        return await original(*args, **kwargs)

    monkeypatch.setattr(
        ScopedEventLedgerUnitOfWork,
        "_find_existing_v2_plugin_external",
        _miss_once,
    )
    replayed = await uow.append_external(
        scope=scope,
        platform_message_id="plugin-ext-race-1",
        source_plugin_id="ext-plugin",
        external_source="github",
        external_event_key="push-race",
        external_event_type="PushEvent",
        external_payload={"ok": True},
        external_target_id="1001",
        content="race-winner",
        occurred_at=_NOW,
    )
    assert calls["n"] == 2
    assert replayed.created is False
    assert replayed.job_signalled is False
    assert replayed.event.id == first.event.id
    assert await _v2_ledger_snapshot(database) == before
    async with database.sessions() as session:
        receipts = list(await session.scalars(select(CanonicalEventReceiptModel)))
        events = list(await session.scalars(select(ChatEventModel)))
        jobs = list(await session.scalars(select(MemoryJobModel)))
        assert receipts == []
        assert len(events) == 1
        assert events[0].id == first.event.id
        assert len(jobs) == 0


async def _v2_group_two_presences(database: Database) -> tuple[object, object, object, str, str]:
    from qq_ai_bot.conversation.rollup.models import RollupPolicyConfig
    from qq_ai_bot.domain.conversations import ConversationScope
    from qq_ai_bot.identity.dual_write import (
        ensure_canonical_person_preconfig,
        ensure_canonical_presence_preconfig,
        ensure_canonical_space_preconfig,
    )
    from qq_ai_bot.persistence.scoped_event_uow import ScopedEventLedgerUnitOfWork

    await _flip_v2(database)
    async with database.sessions() as session, session.begin():
        presence_a = await ensure_canonical_presence_preconfig(session, "8000")
        presence_b = await ensure_canonical_presence_preconfig(session, "8001")
        await ensure_canonical_person_preconfig(session, "1001", now=_NOW)
        await ensure_canonical_space_preconfig(session, "2001", now=_NOW)
    return (
        ScopedEventLedgerUnitOfWork(database, config=RollupPolicyConfig()),
        ConversationScope.group("8000", "2001"),
        ConversationScope.group("8001", "2001"),
        presence_a,
        presence_b,
    )


async def _plant_receipt(
    database: Database,
    *,
    presence_id: str,
    platform_message_id: str,
    canonical_event_id: str,
) -> None:
    from qq_ai_bot.conversation.canonical_db_models import CanonicalEventReceiptModel

    async with database.sessions() as session, session.begin():
        session.add(
            CanonicalEventReceiptModel(
                ingress_presence_id=presence_id,
                event_type="message",
                platform_message_id=platform_message_id,
                canonical_event_id=canonical_event_id,
                created_at=_NOW,
                observed_at=_NOW,
            )
        )


@pytest.mark.asyncio
async def test_v2_live_fanout_receipts_replay_idempotently(database: Database) -> None:
    from qq_ai_bot.conversation.canonical_db_models import CanonicalEventReceiptModel

    uow, scope_a, scope_b, _presence_a, presence_b = await _v2_group_two_presences(database)
    first = await uow.append(
        scope=scope_a,
        platform_message_id="fanout-keep",
        sender_user_id="1001",
        direction="inbound",
        content="shared-body",
        occurred_at=_NOW,
    )
    assert first.created is True
    async with database.sessions() as session:
        keeper = await session.get(ChatEventModel, first.event.id)
        assert keeper is not None and keeper.canonical_event_id
        canonical_event_id = keeper.canonical_event_id
    await _plant_receipt(
        database,
        presence_id=presence_b,
        platform_message_id="fanout-keep",
        canonical_event_id=canonical_event_id,
    )
    before = await _v2_ledger_snapshot(database)
    for scope in (scope_a, scope_b):
        reused = await uow.append(
            scope=scope,
            platform_message_id="fanout-keep",
            sender_user_id="1001",
            direction="inbound",
            content="shared-body",
            occurred_at=_NOW,
        )
        assert reused.created is False
        assert reused.job_signalled is False
        assert reused.event.id == first.event.id
    assert await _v2_ledger_snapshot(database) == before
    async with database.sessions() as session:
        events = list(await session.scalars(select(ChatEventModel)))
        receipts = list(await session.scalars(select(CanonicalEventReceiptModel)))
        assert len(events) == 1
        assert {row.canonical_event_id for row in receipts} == {canonical_event_id}
        assert len(receipts) == 2


@pytest.mark.asyncio
async def test_v2_live_missing_or_forged_receipt_cannot_reuse_keeper_across_bots(
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from qq_ai_bot.identity.errors import IdentityDualWriteError
    from qq_ai_bot.persistence.scoped_event_uow import ScopedEventLedgerUnitOfWork

    uow, scope_a, scope_b, _presence_a, presence_b = await _v2_group_two_presences(database)
    missing = await uow.append(
        scope=scope_a,
        platform_message_id="cross-miss",
        sender_user_id="1001",
        direction="inbound",
        content="keeper-body",
        occurred_at=_NOW,
    )
    independent = await uow.append(
        scope=scope_b,
        platform_message_id="cross-miss",
        sender_user_id="1001",
        direction="inbound",
        content="keeper-body",
        occurred_at=_NOW,
    )
    assert independent.created is True
    assert independent.event.id != missing.event.id
    async with database.sessions() as session:
        keeper = await session.get(ChatEventModel, missing.event.id)
        assert keeper is not None
        assert keeper.content == "keeper-body"
        assert keeper.bot_user_id == "8000"

    forged = await uow.append(
        scope=scope_a,
        platform_message_id="cross-forge",
        sender_user_id="1001",
        direction="inbound",
        content="forge-body",
        occurred_at=_NOW,
    )
    assert forged.created is True
    await _plant_receipt(
        database,
        presence_id=presence_b,
        platform_message_id="cross-forge",
        canonical_event_id=str(uuid4()),
    )
    before_forged = await _v2_ledger_snapshot(database)
    with pytest.raises(IdentityDualWriteError) as forged_exc:
        await uow.append(
            scope=scope_b,
            platform_message_id="cross-forge",
            sender_user_id="1001",
            direction="inbound",
            content="forge-body",
            occurred_at=_NOW,
        )
    assert forged_exc.value.category == "receipt_conflict"
    assert await _v2_ledger_snapshot(database) == before_forged

    keeper_id = missing.event.id

    async def _legacy_hit(
        _self: object,
        session: object,
        *_args: object,
        **_kwargs: object,
    ) -> tuple[ChatEventModel | None, None]:
        existing = await session.get(ChatEventModel, keeper_id)  # type: ignore[union-attr]
        return existing, None

    monkeypatch.setattr(ScopedEventLedgerUnitOfWork, "_find_existing_v2_live", _legacy_hit)
    before_legacy = await _v2_ledger_snapshot(database)
    with pytest.raises(IdentityDualWriteError) as legacy_exc:
        await uow.append(
            scope=scope_b,
            platform_message_id="cross-miss",
            sender_user_id="1001",
            direction="inbound",
            content="keeper-body",
            occurred_at=_NOW,
        )
    assert legacy_exc.value.category == "receipt_conflict"
    assert await _v2_ledger_snapshot(database) == before_legacy


@pytest.mark.asyncio
async def test_v2_live_secondary_receipt_content_or_time_conflict_fails(
    database: Database,
) -> None:
    from qq_ai_bot.identity.errors import IdentityDualWriteError

    uow, scope_a, scope_b, _presence_a, presence_b = await _v2_group_two_presences(database)
    first = await uow.append(
        scope=scope_a,
        platform_message_id="fanout-conflict",
        sender_user_id="1001",
        direction="inbound",
        content="shared-body",
        occurred_at=_NOW,
    )
    async with database.sessions() as session:
        keeper = await session.get(ChatEventModel, first.event.id)
        assert keeper is not None and keeper.canonical_event_id
        canonical_event_id = keeper.canonical_event_id
    await _plant_receipt(
        database,
        presence_id=presence_b,
        platform_message_id="fanout-conflict",
        canonical_event_id=canonical_event_id,
    )
    before = await _v2_ledger_snapshot(database)
    with pytest.raises(IdentityDualWriteError) as content:
        await uow.append(
            scope=scope_b,
            platform_message_id="fanout-conflict",
            sender_user_id="1001",
            direction="inbound",
            content="tampered-body",
            occurred_at=_NOW,
        )
    with pytest.raises(IdentityDualWriteError) as occurred:
        await uow.append(
            scope=scope_b,
            platform_message_id="fanout-conflict",
            sender_user_id="1001",
            direction="inbound",
            content="shared-body",
            occurred_at=_NOW.replace(minute=1),
        )
    assert content.value.category == "receipt_conflict"
    assert occurred.value.category == "receipt_conflict"
    assert await _v2_ledger_snapshot(database) == before
    async with database.sessions() as session:
        row = await session.get(ChatEventModel, first.event.id)
        assert row is not None
        assert row.content == "shared-body"
        stored = row.occurred_at
        if stored.tzinfo is None:
            stored = stored.replace(tzinfo=UTC)
        assert stored == _NOW


def _utterance_token(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


async def _swap_keeper_behind_earlier_duplicate(database: Database, keeper_id: int) -> int:
    async with database.sessions() as session, session.begin():
        original = await session.get(ChatEventModel, keeper_id)
        assert original is not None
        fingerprint = original.utterance_fingerprint or _utterance_token(original.content)
        clone = ChatEventModel(
            bot_user_id=original.bot_user_id,
            platform_message_id=original.platform_message_id,
            scope_type=original.scope_type,
            group_id=original.group_id,
            private_peer_user_id=original.private_peer_user_id,
            sender_user_id=original.sender_user_id,
            sender_nickname=original.sender_nickname,
            sender_group_card=original.sender_group_card,
            direction=original.direction,
            event_kind=original.event_kind,
            content=original.content,
            visual_summary=original.visual_summary,
            segments_json=original.segments_json,
            origin=original.origin,
            occurred_at=original.occurred_at,
            observed_at=original.observed_at,
            canonical_event_id=original.canonical_event_id,
            canonical_conversation_id=original.canonical_conversation_id,
            author_kind=original.author_kind,
            author_person_id=original.author_person_id,
            author_presence_id=original.author_presence_id,
            ingress_presence_id=original.ingress_presence_id,
            utterance_fingerprint=fingerprint,
            suppression_status="duplicate",
            ingress_provider=original.ingress_provider,
            ingress_gateway_instance_id=original.ingress_gateway_instance_id,
        )
        session.add(clone)
        await session.flush()
        assert clone.id > original.id
        original.utterance_fingerprint = fingerprint
        original.suppression_status = "duplicate"
        clone.suppression_status = "keeper"
        return clone.id


@pytest.mark.asyncio
async def test_v2_live_receipts_reuse_keeper_when_duplicate_has_smaller_id(
    database: Database,
) -> None:
    from qq_ai_bot.conversation.canonical_db_models import CanonicalEventReceiptModel

    uow, scope_a, scope_b, _presence_a, presence_b = await _v2_group_two_presences(database)
    first = await uow.append(
        scope=scope_a,
        platform_message_id="fanout-keeper-order",
        sender_user_id="1001",
        direction="inbound",
        content="shared-body",
        occurred_at=_NOW,
    )
    async with database.sessions() as session:
        seeded = await session.get(ChatEventModel, first.event.id)
        assert seeded is not None and seeded.canonical_event_id
        canonical_event_id = seeded.canonical_event_id
    keeper_id = await _swap_keeper_behind_earlier_duplicate(database, first.event.id)
    assert keeper_id > first.event.id
    await _plant_receipt(
        database,
        presence_id=presence_b,
        platform_message_id="fanout-keeper-order",
        canonical_event_id=canonical_event_id,
    )
    for scope in (scope_a, scope_b):
        reused = await uow.append(
            scope=scope,
            platform_message_id="fanout-keeper-order",
            sender_user_id="1001",
            direction="inbound",
            content="shared-body",
            occurred_at=_NOW,
        )
        assert reused.created is False
        assert reused.event.id == keeper_id
        assert reused.event.id != first.event.id
    async with database.sessions() as session:
        rows = list(await session.scalars(select(ChatEventModel)))
        receipts = list(await session.scalars(select(CanonicalEventReceiptModel)))
        keepers = [row for row in rows if row.suppression_status == "keeper"]
    assert len(keepers) == 1
    assert keepers[0].id == keeper_id
    assert {item.canonical_event_id for item in receipts} == {canonical_event_id}
    assert len(receipts) == 2


@pytest.mark.asyncio
async def test_v2_live_receipt_with_only_duplicate_fails_closed(database: Database) -> None:
    from qq_ai_bot.identity.errors import IdentityDualWriteError

    uow, scope = await _v2_private_ledger(database)
    first = await uow.append(
        scope=scope,
        platform_message_id="dup-only-1",
        sender_user_id="1001",
        direction="inbound",
        content="kept-body",
        occurred_at=_NOW,
    )
    async with database.sessions() as session, session.begin():
        row = await session.get(ChatEventModel, first.event.id)
        assert row is not None
        row.utterance_fingerprint = row.utterance_fingerprint or _utterance_token(row.content)
        row.suppression_status = "duplicate"
    before = await _v2_ledger_snapshot(database)
    with pytest.raises(IdentityDualWriteError) as exc:
        await uow.append(
            scope=scope,
            platform_message_id="dup-only-1",
            sender_user_id="1001",
            direction="inbound",
            content="kept-body",
            occurred_at=_NOW,
        )
    assert exc.value.category == "receipt_conflict"
    assert "kept-body" not in str(exc.value)
    assert await _v2_ledger_snapshot(database) == before


def _turn(
    *,
    scope_key: str,
    transport_scope_key: str | None = None,
    scope_id: int = 7,
    generation: int = 1,
) -> ConversationTurnSnapshot:
    return ConversationTurnSnapshot(
        scope_id=scope_id,
        scope_key=scope_key,
        generation=generation,
        trigger_event_id=3,
        coordinator_version=1,
        transport_scope_key=transport_scope_key,
    )


def test_turn_matches_hydrated_scope_when_primary_equals_current() -> None:
    key = "bot:8000:private:1001"
    turn = _turn(scope_key=key)
    assert turn.transport_scope_key is None
    assert turn_matches_hydrated_scope(
        turn,
        scope_id=7,
        generation=1,
        transport_key=key,
        runtime_key=key,
    )
    assert turn_matches_hydrated_scope(
        turn,
        scope_id=7,
        generation=1,
        transport_key=key,
        runtime_key=None,
    )


def test_turn_matches_hydrated_scope_rejects_secondary_when_primary_equals_current() -> None:
    primary = "bot:8000:private:1001"
    secondary = "bot:8001:private:1001"
    turn = _turn(scope_key=primary)
    assert turn.transport_scope_key is None
    assert not turn_matches_hydrated_scope(
        turn,
        scope_id=7,
        generation=1,
        transport_key=secondary,
        runtime_key=primary,
    )


def test_turn_matches_hydrated_scope_when_primary_differs_from_current() -> None:
    primary = "bot:8000:private:1001"
    current = "bot:8001:private:1001"
    turn = _turn(scope_key=primary, transport_scope_key=current)
    assert turn_matches_hydrated_scope(
        turn,
        scope_id=7,
        generation=1,
        transport_key=current,
        runtime_key=primary,
    )


def test_turn_matches_hydrated_scope_rejects_forged_primary_or_wrong_transport() -> None:
    primary = "bot:8000:private:1001"
    current = "bot:8001:private:1001"
    turn = _turn(scope_key=primary, transport_scope_key=current)
    assert not turn_matches_hydrated_scope(
        turn,
        scope_id=7,
        generation=1,
        transport_key=current,
        runtime_key="bot:9999:private:1001",
    )
    assert not turn_matches_hydrated_scope(
        turn,
        scope_id=7,
        generation=1,
        transport_key="bot:8002:private:1001",
        runtime_key=primary,
    )


def test_turn_matches_hydrated_scope_rejects_v1_key_mismatch() -> None:
    turn = _turn(scope_key="bot:8000:private:1001")
    assert not turn_matches_hydrated_scope(
        turn,
        scope_id=7,
        generation=1,
        transport_key="bot:8000:private:1002",
        runtime_key=None,
    )


def test_conversation_scope_parse_round_trips_private_and_group() -> None:
    private = ConversationScope.private("8001", "1001")
    group = ConversationScope.group("8001", "2001")
    assert ConversationScope.parse(private.key) == private
    assert ConversationScope.parse(group.key) == group


def test_conversation_scope_parse_rejects_invalid_keys() -> None:
    with pytest.raises(ValueError, match="invalid"):
        ConversationScope.parse("private:1001")
    with pytest.raises(ValueError, match="invalid"):
        ConversationScope.parse("bot:8000:channel:1001")
