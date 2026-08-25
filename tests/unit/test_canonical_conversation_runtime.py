"""C19-C24 canonical conversation, memory, automation, plugin, and scope."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select

from qq_ai_bot.automation.models import TurnOrigin
from qq_ai_bot.conversation.canonical_db_models import ConversationLegacyAliasModel
from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.domain.messages import InboundMessage, SenderIdentity
from qq_ai_bot.gateway.registry import GatewayConnectionRegistry
from qq_ai_bot.identity.canonical_uow import CanonicalIngressUnitOfWork
from qq_ai_bot.identity.db_models import IdentityRuntimeStateModel
from qq_ai_bot.identity.ingress import CanonicalIngressResolver, ensure_v2_presence
from qq_ai_bot.identity.inventory import DEFERRED_SHADOWS, FILLABLE_SHADOWS
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
from qq_ai_bot.persistence.models import MemoryFactModel
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
    registry = GatewayConnectionRegistry(gateway_instance_id="gw-conv")
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
        from qq_ai_bot.identity.dual_write import ensure_runtime_people_row

        await ensure_runtime_people_row(session, "1001", now=_NOW)
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
    registry = GatewayConnectionRegistry(gateway_instance_id="gw-auto")
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
    registry = GatewayConnectionRegistry(gateway_instance_id="gw-plugin")
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
    from qq_ai_bot.identity.dual_write import ensure_runtime_people_row
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
        await ensure_runtime_people_row(session, "1001", now=_NOW)
        await ensure_runtime_people_row(session, "8000", is_bot=True, now=_NOW)
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
    registry = GatewayConnectionRegistry(gateway_instance_id="gw-auto-persist")
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
    deferred_text = " ".join(deferred)
    assert "canonical_conversation_id" in deferred_text
    assert "automations.canonical_target_person_id/space_id" not in deferred_text
    assert "memory_jobs" in deferred
    assert "memory_evidence" in deferred
    assert "memory_tool_receipts" in deferred
    assert "memory_reflection_jobs" in deferred
    assert "memory_self_reflection_states/memory_self_reflection_runs" in deferred
    assert "memory_dream_runs/memory_dream_clusters/memory_dream_operations" in deferred
