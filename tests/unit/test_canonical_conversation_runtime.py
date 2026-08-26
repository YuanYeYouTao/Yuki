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
from qq_ai_bot.identity.canonical_repository import (
    ensure_person,
    ensure_space,
)
from qq_ai_bot.identity.canonical_repository import (
    ensure_presence as ensure_v2_presence,
)
from qq_ai_bot.identity.canonical_uow import CanonicalIngressUnitOfWork
from qq_ai_bot.identity.ingress import CanonicalIngressResolver
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


@dataclass
class _Bot:
    self_id: str

    async def call_api(self, *_args: object, **_kwargs: object) -> dict[str, object]:
        return {}


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
    fallback_identity = ConversationScope.private("8001", "1001")
    fallback_message = InboundMessage(
        message_id="cmd-fallback",
        event_type="message:test",
        scope_type=ScopeType.PRIVATE,
        sender=SenderIdentity(user_id="1001"),
        text="hi",
        bot_user_id="8001",
    )
    assert plugin_conversation_key(fallback_message, fallback_identity) == fallback_identity.key


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

    fallback_inbound = InboundMessage(
        message_id="facade-fallback",
        event_type="message:test",
        scope_type=ScopeType.PRIVATE,
        sender=SenderIdentity(user_id="1001"),
        text="hi",
        bot_user_id="8001",
    )
    fallback_invocation = PluginInvocation(
        plugin_id="demo.plugin",
        origin=TurnOrigin.USER_MESSAGE,
        actor_user_id="1001",
        bot_user_id="8001",
        inbound=fallback_inbound,
    )
    assert fallback_invocation.conversation_key == ConversationScope.private("8001", "1001").key
    assert (
        ConversationTurnCoordinator.key_for(fallback_inbound)
        == fallback_invocation.conversation_key
    )


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
        target_type="private",
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
            target_type="private",
            text="missing",
        )
    assert missing.value.category == "none"


def _chat_event(
    *,
    platform_message_id: str,
    content: str,
    canonical_event_id: str | None = None,
    canonical_conversation_id: str | None = None,
    author_person_id: str,
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
        author_kind="person",
        author_person_id=author_person_id,
        suppression_status="keeper",
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
) -> tuple[str, str]:
    from qq_ai_bot.conversation.canonical_db_models import (
        CanonicalConversationModel,
        ConversationLegacyAliasModel,
    )
    from qq_ai_bot.identity.ingress import _ensure_person_id

    person_id = await _ensure_person_id(session, "1001")  # type: ignore[arg-type]
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
    return conversation_id, person_id


@pytest.mark.asyncio
async def test_v2_refuse_legacy_live_event_uses_conversation_watermark(
    database: Database,
) -> None:
    from qq_ai_bot.identity.memory_guard import refuse_legacy_live_event

    configure_identity_write_settings(IdentityWriteSettings(superusers=frozenset({"9000"})))
    async with database.sessions() as session, session.begin():
        conversation_id, person_id = await _seed_guard_conversation(
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
            author_person_id=person_id,
            event_id=5,
        )
        fresh = _chat_event(
            platform_message_id="cutover-new",
            content="post cutover",
            canonical_event_id=str(uuid4()),
            canonical_conversation_id=conversation_id,
            author_person_id=person_id,
            event_id=10,
        )
        session.add_all([old, fresh])
        await session.flush()
        assert await refuse_legacy_live_event(session, old)
        assert not await refuse_legacy_live_event(session, fresh)
        old_pk = old.id
        fresh_pk = fresh.id
    jobs = MemoryJobRepository(database)
    assert await jobs.enqueue(old_pk, "private:1001") is False
    assert await jobs.enqueue(fresh_pk, "private:1001") is True


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
    async with database.sessions() as session, session.begin():
        await ensure_person(session, "1808058482", now=_NOW)
    row = await service.create(script, inbound=inbound, conversation_key="private:9000")
    assert row.canonical_target_person_id is not None
    assert row.canonical_target_person_id != row.canonical_creator_person_id
    configure_identity_write_settings(IdentityWriteSettings(superusers=frozenset({"9000"})))
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
    from qq_ai_bot.persistence.repositories import PeopleRepository
    from qq_ai_bot.persistence.scoped_event_uow import ScopedEventLedgerUnitOfWork

    async with database.sessions() as session, session.begin():
        await ensure_v2_presence(session, "8000")
        await ensure_person(session, "1001", now=_NOW)
        await ensure_space(session, "2001", now=_NOW)
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
    from qq_ai_bot.persistence.scoped_event_uow import ScopedEventLedgerUnitOfWork

    async with database.sessions() as session, session.begin():
        await ensure_v2_presence(session, "8000")
        await ensure_person(session, "1001", now=_NOW)
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
@pytest.mark.asyncio
@pytest.mark.asyncio
@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_v2_plugin_external_stays_off_receipts_and_keeps_unique_key(
    database: Database,
) -> None:
    from qq_ai_bot.conversation.canonical_db_models import CanonicalEventReceiptModel
    from qq_ai_bot.identity.errors import CanonicalIdentityError

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

    with pytest.raises(CanonicalIdentityError) as platform_conflict:
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

    with pytest.raises(CanonicalIdentityError) as payload_conflict:
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

    with pytest.raises(CanonicalIdentityError) as content_conflict:
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
    from qq_ai_bot.identity.errors import CanonicalIdentityError

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
    with pytest.raises(CanonicalIdentityError) as exc:
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
    assert str(exc.value) == "canonical identity invariant failed"
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
async def _v2_group_two_presences(database: Database) -> tuple[object, object, object, str, str]:
    from qq_ai_bot.conversation.rollup.models import RollupPolicyConfig
    from qq_ai_bot.domain.conversations import ConversationScope
    from qq_ai_bot.persistence.scoped_event_uow import ScopedEventLedgerUnitOfWork

    async with database.sessions() as session, session.begin():
        presence_a = await ensure_v2_presence(session, "8000")
        presence_b = await ensure_v2_presence(session, "8001")
        await ensure_person(session, "1001", now=_NOW)
        await ensure_space(session, "2001", now=_NOW)
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
@pytest.mark.asyncio
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
@pytest.mark.asyncio
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


def test_turn_matches_hydrated_scope_rejects_transport_key_mismatch() -> None:
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
