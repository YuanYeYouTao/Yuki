"""complete-v2 Processor hot-path regressions for P0-A/P0-B.

These tests exercise the real MessageProcessor.handle path. They must fail on
the unfixed HEAD: v2 observe/capture still require_v1, and a non-primary
Presence chat turn raises ConversationCoverageError.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from tests.conftest import MemorySender, build_harness, make_settings
from tests.support.gateway import napcat_registry

from qq_ai_bot.conversation.canonical_db_models import ConversationLegacyAliasModel
from qq_ai_bot.conversation.rollup.db_models import ConversationScopeModel
from qq_ai_bot.domain.conversations import ConversationScope, ScopeType
from qq_ai_bot.domain.identity import AuthorKind
from qq_ai_bot.domain.messages import InboundMessage, SenderIdentity
from qq_ai_bot.gateway.registry import GatewayConnectionRegistry
from qq_ai_bot.identity.canonical_uow import CanonicalIngressUnitOfWork
from qq_ai_bot.identity.db_models import (
    CanonicalPersonModel,
    IdentityBindingModel,
    IdentityRuntimeStateModel,
    SpaceBindingModel,
)
from qq_ai_bot.identity.dual_write import ensure_canonical_presence_preconfig as ensure_v2_presence
from qq_ai_bot.identity.dual_write import ensure_v2_space
from qq_ai_bot.identity.errors import IdentityDualWriteError
from qq_ai_bot.identity.ingress import CanonicalIngressResolver, IngressPreAdmit
from qq_ai_bot.identity.inventory import IDENTITY_PLATFORM
from qq_ai_bot.identity.routing import PresenceRouter
from qq_ai_bot.llm.fake import FakeLLMProvider
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.models import (
    ChatEventModel,
    GroupModel,
    PersonModel,
    RuntimeTurnObservationModel,
)
from qq_ai_bot.persistence.turn_observations import RuntimeTurnObservationRepository
from qq_ai_bot.runtime.observability import (
    RuntimeTurnCorrelation,
    build_turn_observation,
    claim_runtime_turn_id,
    hash_conversation_key,
    new_runtime_turn_id,
)
from qq_ai_bot.runtime.origin import TurnOrigin as ObservationTurnOrigin
from qq_ai_bot.services.chat import ChatService
from qq_ai_bot.services.context_assembler import AssembledContext
from qq_ai_bot.services.turn_coordinator import ConversationTurnCoordinator
from qq_ai_bot.web.models import WebSearchResponse, WebSearchSource

_NOW = datetime(2026, 8, 24, tzinfo=UTC)
_CUTOVER = "550e8400-e29b-41d4-a716-446655440099"


@dataclass
class _Bot:
    self_id: str

    async def call_api(self, *_args: object, **_kwargs: object) -> dict[str, object]:
        return {}


class IngressSender(MemorySender):
    def __init__(self, bot: _Bot) -> None:
        super().__init__()
        self.bot = bot


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


def _inbound(
    *,
    message_id: str,
    user_id: str = "1001",
    bot_user_id: str = "8000",
    group_id: str | None = None,
    text: str = "hello",
    mentions_bot: bool = False,
    nickname: str = "远野",
    mentioned_user_ids: tuple[str, ...] = (),
    reply_to_message_id: str | None = None,
    reply_sender_user_id: str | None = None,
    is_self_message: bool = False,
    is_bot: bool = False,
) -> InboundMessage:
    return InboundMessage(
        message_id=message_id,
        event_type="message:test",
        scope_type=ScopeType.GROUP if group_id else ScopeType.PRIVATE,
        sender=SenderIdentity(user_id=user_id, nickname=nickname, is_bot=is_bot),
        text=text,
        bot_user_id=bot_user_id,
        group_id=group_id,
        mentions_bot=mentions_bot,
        mentioned_user_ids=mentioned_user_ids,
        reply_to_message_id=reply_to_message_id,
        reply_sender_user_id=reply_sender_user_id,
        is_self_message=is_self_message,
    )


async def _carrier_counts(database: Database) -> tuple[int, int, int]:
    async with database.sessions() as session:
        people = int(await session.scalar(select(func.count()).select_from(PersonModel)) or 0)
        groups = int(await session.scalar(select(func.count()).select_from(GroupModel)) or 0)
        scopes = int(
            await session.scalar(select(func.count()).select_from(ConversationScopeModel)) or 0
        )
    return people, groups, scopes


def _coordinator_keys(harness: object) -> set[str]:
    return set(harness.processor._turn_coordinator._states)


def _session_keys(harness: object) -> set[str]:
    return set(harness.concurrency._locks)


async def _observation_rows(database: Database) -> list[RuntimeTurnObservationModel]:
    async with database.sessions() as session:
        return list(
            await session.scalars(
                select(RuntimeTurnObservationModel).order_by(RuntimeTurnObservationModel.id.asc())
            )
        )


async def _observation_hashes(database: Database) -> set[str]:
    return {
        row.conversation_key_hash
        for row in await _observation_rows(database)
        if row.conversation_key_hash
    }


async def _person_id_for(database: Database, external_id: str) -> str:
    async with database.sessions() as session:
        binding = await session.scalar(
            select(IdentityBindingModel).where(
                IdentityBindingModel.external_account_id == external_id
            )
        )
    assert binding is not None
    return binding.person_id


async def _space_id_for(database: Database, group_id: str) -> str:
    async with database.sessions() as session:
        binding = await session.scalar(
            select(SpaceBindingModel).where(SpaceBindingModel.external_space_id == group_id)
        )
    assert binding is not None
    return binding.space_id


def _assert_single_internal_key(harness: object, primary: str, secondary: str) -> None:
    assert _coordinator_keys(harness) == {primary}
    assert secondary not in _coordinator_keys(harness)
    assert primary in _session_keys(harness)
    assert secondary not in _session_keys(harness)


async def _seed_web_run(harness: object, conversation_key: str, trigger_message_id: str) -> None:
    await harness.processor._chat._web_sources.save_response(
        conversation_key=conversation_key,
        trigger_message_id=trigger_message_id,
        provider="test",
        response=WebSearchResponse(
            query="seeded sources",
            provider_request_id="seed-1",
            latency_seconds=0.01,
            sources=(
                WebSearchSource(
                    source_id="seed-source",
                    title="seeded title",
                    url="https://example.com/seed",
                    domain="example.com",
                    snippet="seeded snippet",
                    relevant_content="seeded snippet",
                ),
            ),
        ),
        max_runs=5,
    )


@dataclass
class _CountingIngress:
    inner: CanonicalIngressResolver
    calls: int = 0

    async def pre_admit(
        self, bot: object | None, message: InboundMessage
    ) -> IngressPreAdmit | None:
        self.calls += 1
        return await self.inner.pre_admit(bot, message)


def _wire_ingress(
    harness: object, database: Database
) -> tuple[GatewayConnectionRegistry, PresenceRouter]:
    registry = napcat_registry(gateway_instance_id="gw-v2-proc")
    router = PresenceRouter(database, registry, membership_probe=_true)
    resolver = CanonicalIngressResolver(database, registry, router)
    uow = CanonicalIngressUnitOfWork(database, router)
    harness.processor._canonical_ingress = resolver
    harness.processor._canonical_uow = uow
    return registry, router


@pytest.mark.asyncio
async def test_complete_v2_private_handle_reaches_fake_chat_without_carriers(
    database: Database,
) -> None:
    settings = make_settings("sqlite+aiosqlite:///:memory:")
    provider = FakeLLMProvider()
    harness = build_harness(database, settings, provider)
    registry, _router = _wire_ingress(harness, database)
    await _flip_v2(database)
    bot = _Bot("8000")
    async with database.sessions() as session, session.begin():
        presence = await ensure_v2_presence(session, "8000")
    registry.connect(bot)
    registry.bind_presence(platform="qq", external_account_id="8000", presence_id=presence)
    before = await _carrier_counts(database)
    sender = IngressSender(bot)
    result = await harness.processor.handle(_inbound(message_id="p-v2-1"), sender)
    assert result.handled is True
    assert result.sent_messages >= 1
    assert sender.messages
    assert sender.messages[0].text.startswith("FakeLLM:")
    assert await _carrier_counts(database) == before
    async with database.sessions() as session:
        events = list(await session.scalars(select(ChatEventModel)))
    inbound_events = [row for row in events if row.direction == "inbound"]
    assert len(inbound_events) == 1
    assert inbound_events[0].bot_user_id == "8000"


@pytest.mark.asyncio
async def test_complete_v2_enabled_group_handle_does_not_write_carriers(
    database: Database,
) -> None:
    settings = make_settings("sqlite+aiosqlite:///:memory:")
    harness = build_harness(database, settings)
    registry, _router = _wire_ingress(harness, database)
    await _flip_v2(database)
    bot = _Bot("8000")
    async with database.sessions() as session, session.begin():
        presence = await ensure_v2_presence(session, "8000")
        await ensure_v2_space(session, "2001")
    registry.connect(bot)
    registry.bind_presence(platform="qq", external_account_id="8000", presence_id=presence)
    before = await _carrier_counts(database)
    sender = IngressSender(bot)
    result = await harness.processor.handle(
        _inbound(message_id="g-v2-1", group_id="2001", mentions_bot=True, text="群里问好"),
        sender,
    )
    assert result.handled is True
    assert result.sent_messages >= 1
    assert await _carrier_counts(database) == before


@pytest.mark.asyncio
async def test_complete_v2_second_presence_keeps_conversation_lock_and_history(
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = make_settings("sqlite+aiosqlite:///:memory:")
    harness = build_harness(database, settings)
    registry, _router = _wire_ingress(harness, database)
    await _flip_v2(database)
    bot_a = _Bot("8000")
    bot_b = _Bot("8001")
    async with database.sessions() as session, session.begin():
        presence_a = await ensure_v2_presence(session, "8000")
        presence_b = await ensure_v2_presence(session, "8001")
    registry.connect(bot_a)
    registry.bind_presence(platform="qq", external_account_id="8000", presence_id=presence_a)
    first = await harness.processor.handle(
        _inbound(message_id="p-switch-1", text="第一句"),
        IngressSender(bot_a),
    )
    assert first.handled is True
    registry.connect(bot_b)
    registry.bind_presence(platform="qq", external_account_id="8001", presence_id=presence_b)
    seen: list[tuple[str, str, object]] = []
    original = ChatService._run_agent
    primary = ConversationScope.private("8000", "1001").key

    async def _spy(
        self: ChatService,
        conversation_key: str,
        messages: object,
        runtime: object,
    ) -> object:
        seen.append((conversation_key, runtime.conversation_key, messages))
        assert conversation_key == primary
        assert runtime.conversation_key == conversation_key
        assert not harness.concurrency.is_processing(ConversationScope.private("8001", "1001").key)
        assert claim_runtime_turn_id() is not None
        return await original(self, conversation_key, messages, runtime)

    monkeypatch.setattr(ChatService, "_run_agent", _spy)
    second = await harness.processor.handle(
        _inbound(message_id="p-switch-2", bot_user_id="8001", text="第二句"),
        IngressSender(bot_b),
    )
    assert second.handled is True
    assert second.reason == "chat"
    assert second.sent_messages > 0
    assert seen
    prompt = "\n".join(str(getattr(item, "content", item) or "") for item in seen[0][2])
    assert "第一句" in prompt
    async with database.sessions() as session:
        rows = list(await session.scalars(select(ChatEventModel).order_by(ChatEventModel.id.asc())))
        aliases = list(await session.scalars(select(ConversationLegacyAliasModel)))
    inbound_rows = [row for row in rows if row.direction == "inbound"]
    outbound_rows = [row for row in rows if row.direction == "outbound"]
    assert [row.bot_user_id for row in inbound_rows] == ["8000", "8001"]
    assert outbound_rows[-1].bot_user_id == "8001"
    assert {row.canonical_conversation_id for row in inbound_rows} == {
        inbound_rows[0].canonical_conversation_id
    }
    assert {item.scope_key for item in aliases} == {
        "bot:8000:private:1001",
        "bot:8001:private:1001",
    }
    primaries = [item.scope_key for item in aliases if item.is_primary == 1]
    assert primaries == ["bot:8000:private:1001"]
    secondary = ConversationScope.private("8001", "1001").key
    _assert_single_internal_key(harness, primary, secondary)
    observations = await _observation_rows(database)
    assert len(observations) == 1
    observation = observations[0]
    person_id = await _person_id_for(database, "1001")
    assert observation.conversation_key_hash == hash_conversation_key(primary)
    assert observation.canonical_conversation_id == inbound_rows[1].canonical_conversation_id
    assert observation.canonical_person_id == person_id
    assert observation.canonical_space_id is None
    await _seed_web_run(harness, primary, "p-switch-1")
    web_keys: list[str] = []
    original_latest = harness.processor._chat._web_sources.latest

    async def _latest(conversation_key: str) -> object:
        web_keys.append(conversation_key)
        return await original_latest(conversation_key)

    monkeypatch.setattr(harness.processor._chat._web_sources, "latest", _latest)
    sources = await harness.processor.handle(
        _inbound(message_id="p-switch-3", bot_user_id="8001", text="来源"),
        IngressSender(bot_b),
    )
    assert sources.handled is True
    assert web_keys == [primary]
    _assert_single_internal_key(harness, primary, secondary)
    async with database.sessions() as session:
        inbound_again = list(
            await session.scalars(
                select(ChatEventModel)
                .where(ChatEventModel.direction == "inbound")
                .order_by(ChatEventModel.id.asc())
            )
        )
    assert [row.bot_user_id for row in inbound_again] == ["8000", "8001", "8001"]
    assert inbound_again[-1].bot_user_id == "8001"


@pytest.mark.asyncio
async def test_complete_v2_space_takeover_keeps_runtime_key_and_new_bot_id(
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = make_settings("sqlite+aiosqlite:///:memory:")
    harness = build_harness(database, settings)
    registry, router = _wire_ingress(harness, database)
    await _flip_v2(database)
    bot_a = _Bot("8000")
    bot_b = _Bot("8001")
    async with database.sessions() as session, session.begin():
        presence_a = await ensure_v2_presence(session, "8000")
        presence_b = await ensure_v2_presence(session, "8001")
        await ensure_v2_space(session, "2001")
    registry.connect(bot_a)
    registry.bind_presence(platform="qq", external_account_id="8000", presence_id=presence_a)
    first = await harness.processor.handle(
        _inbound(message_id="g-take-1", group_id="2001", mentions_bot=True, text="接管前"),
        IngressSender(bot_a),
    )
    assert first.handled is True
    registry.connect(bot_b)
    registry.bind_presence(platform="qq", external_account_id="8001", presence_id=presence_b)
    registry.disconnect(bot_a)
    async with database.sessions() as session:
        binding = await session.scalar(select(SpaceBindingModel))
        assert binding is not None
        space_binding_id = binding.id
    status = await router._provision_ingest_route(
        space_binding_id,
        event_presence_id=None,
    )
    assert status == "ok"
    seen: list[tuple[str, str, object]] = []
    original = ChatService._run_agent
    primary = ConversationScope.group("8000", "2001").key

    async def _spy(
        self: ChatService,
        conversation_key: str,
        messages: object,
        runtime: object,
    ) -> object:
        seen.append((conversation_key, runtime.conversation_key, messages))
        assert conversation_key == primary
        assert runtime.conversation_key == conversation_key
        assert not harness.concurrency.is_processing(ConversationScope.group("8001", "2001").key)
        assert claim_runtime_turn_id() is not None
        return await original(self, conversation_key, messages, runtime)

    monkeypatch.setattr(ChatService, "_run_agent", _spy)
    second = await harness.processor.handle(
        _inbound(
            message_id="g-take-2",
            group_id="2001",
            bot_user_id="8001",
            mentions_bot=True,
            text="接管后",
        ),
        IngressSender(bot_b),
    )
    assert second.handled is True
    assert second.reason == "chat"
    assert second.sent_messages > 0
    assert seen
    prompt = "\n".join(str(getattr(item, "content", item) or "") for item in seen[0][2])
    assert "接管前" in prompt
    async with database.sessions() as session:
        rows = list(await session.scalars(select(ChatEventModel).order_by(ChatEventModel.id.asc())))
    inbound_rows = [row for row in rows if row.direction == "inbound"]
    outbound_rows = [row for row in rows if row.direction == "outbound"]
    assert [row.bot_user_id for row in inbound_rows] == ["8000", "8001"]
    assert outbound_rows[-1].bot_user_id == "8001"
    assert inbound_rows[0].canonical_conversation_id == inbound_rows[1].canonical_conversation_id
    secondary = ConversationScope.group("8001", "2001").key
    _assert_single_internal_key(harness, primary, secondary)
    observations = await _observation_rows(database)
    assert len(observations) == 1
    observation = observations[0]
    space_id = await _space_id_for(database, "2001")
    assert observation.conversation_key_hash == hash_conversation_key(primary)
    assert observation.canonical_conversation_id == inbound_rows[1].canonical_conversation_id
    assert observation.canonical_space_id == space_id
    assert observation.canonical_person_id is None
    await _seed_web_run(harness, primary, "g-take-1")
    web_keys: list[str] = []
    original_latest = harness.processor._chat._web_sources.latest

    async def _latest(conversation_key: str) -> object:
        web_keys.append(conversation_key)
        return await original_latest(conversation_key)

    monkeypatch.setattr(harness.processor._chat._web_sources, "latest", _latest)
    sources = await harness.processor.handle(
        _inbound(
            message_id="g-take-3",
            group_id="2001",
            bot_user_id="8001",
            mentions_bot=True,
            text="来源",
        ),
        IngressSender(bot_b),
    )
    assert sources.handled is True
    assert web_keys == [primary]
    _assert_single_internal_key(harness, primary, secondary)


@pytest.mark.asyncio
async def test_complete_v2_secondary_alias_cannot_open_second_coordinator_state(
    database: Database,
) -> None:
    settings = make_settings("sqlite+aiosqlite:///:memory:")
    harness = build_harness(database, settings)
    registry, _router = _wire_ingress(harness, database)
    await _flip_v2(database)
    bot_a = _Bot("8000")
    bot_b = _Bot("8001")
    async with database.sessions() as session, session.begin():
        presence_a = await ensure_v2_presence(session, "8000")
        presence_b = await ensure_v2_presence(session, "8001")
    registry.connect(bot_a)
    registry.bind_presence(platform="qq", external_account_id="8000", presence_id=presence_a)
    first = await harness.processor.handle(
        _inbound(message_id="p-coord-1", text="先占锁"),
        IngressSender(bot_a),
    )
    assert first.handled is True
    registry.connect(bot_b)
    registry.bind_presence(platform="qq", external_account_id="8001", presence_id=presence_b)
    primary = ConversationScope.private("8000", "1001").key
    secondary = ConversationScope.private("8001", "1001").key
    hydrated = _inbound(message_id="p-coord-2", bot_user_id="8001", text="次级别名")
    admitted = await harness.processor._canonical_ingress.pre_admit(bot_b, hydrated)
    assert admitted is not None and not admitted.dropped
    assert ConversationTurnCoordinator.key_for(admitted.message) == primary
    assert ConversationTurnCoordinator.key_for(admitted.message) != secondary
    second = await harness.processor.handle(hydrated, IngressSender(bot_b))
    assert second.handled is True
    _assert_single_internal_key(harness, primary, secondary)
    assert ConversationTurnCoordinator.key_for(hydrated) == secondary


@pytest.mark.asyncio
async def test_v1_processor_keeps_transport_runtime_keys(
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = make_settings("sqlite+aiosqlite:///:memory:")
    harness = build_harness(database, settings)
    first_key = ConversationScope.private("8000", "1001").key
    second_key = ConversationScope.private("8001", "1001").key
    original = ChatService._run_agent

    async def _touch(
        self: ChatService,
        conversation_key: str,
        messages: object,
        runtime: object,
    ) -> object:
        assert claim_runtime_turn_id() is not None
        return await original(self, conversation_key, messages, runtime)

    monkeypatch.setattr(ChatService, "_run_agent", _touch)
    first = await harness.processor.handle(
        _inbound(message_id="v1-p-1", text="v1 第一句"),
        IngressSender(_Bot("8000")),
    )
    assert first.handled is True
    assert first.reason == "chat"
    assert _coordinator_keys(harness) == {first_key}
    assert first_key in _session_keys(harness)
    second = await harness.processor.handle(
        _inbound(message_id="v1-p-2", bot_user_id="8001", text="v1 另一账号"),
        IngressSender(_Bot("8001")),
    )
    assert second.handled is True
    assert _coordinator_keys(harness) == {first_key, second_key}
    hashes = await _observation_hashes(database)
    assert hashes == {hash_conversation_key(first_key), hash_conversation_key(second_key)}
    async with database.sessions() as session:
        inbound_rows = [
            row
            for row in await session.scalars(select(ChatEventModel))
            if row.direction == "inbound"
        ]
    assert [row.bot_user_id for row in inbound_rows] == ["8000", "8001"]
    assert all(row.canonical_conversation_id is None for row in inbound_rows)
    v1_rows = await _observation_rows(database)
    assert len(v1_rows) == 2
    assert all(row.canonical_conversation_id is None for row in v1_rows)
    assert all(row.canonical_person_id is None for row in v1_rows)
    assert all(row.canonical_space_id is None for row in v1_rows)


@pytest.mark.asyncio
async def test_observation_recorder_does_not_map_yuki_presence_to_person(
    database: Database,
) -> None:
    await _flip_v2(database)
    async with database.sessions() as session, session.begin():
        await ensure_v2_presence(session, "8000")
    before = await _carrier_counts(database)
    observation = build_turn_observation(
        RuntimeTurnCorrelation(
            turn_id=new_runtime_turn_id(),
            origin=ObservationTurnOrigin.USER_MESSAGE,
        ),
        scope_type="private",
        conversation_key=ConversationScope.private("8000", "8000").key,
        admission_outcome="chat",
        handled=True,
        sent_messages=1,
        error_category=None,
        total_latency_ms=1,
        subject_user_id="8000",
    )
    await RuntimeTurnObservationRepository(database).record_turn(observation)
    rows = await _observation_rows(database)
    assert len(rows) == 1
    assert rows[0].canonical_person_id is None
    assert rows[0].canonical_space_id is None
    assert rows[0].canonical_conversation_id is None
    assert await _carrier_counts(database) == before
    async with database.sessions() as session:
        people = list(await session.scalars(select(PersonModel)))
        bindings = list(
            await session.scalars(
                select(IdentityBindingModel).where(
                    IdentityBindingModel.external_account_id == "8000"
                )
            )
        )
    assert people == []
    assert bindings == []


@pytest.mark.asyncio
async def test_observation_recorder_fail_closes_unknown_canonical_person(
    database: Database,
) -> None:
    observation = build_turn_observation(
        RuntimeTurnCorrelation(
            turn_id=new_runtime_turn_id(),
            origin=ObservationTurnOrigin.USER_MESSAGE,
        ),
        scope_type="private",
        conversation_key=ConversationScope.private("8000", "1001").key,
        admission_outcome="chat",
        handled=True,
        sent_messages=1,
        error_category=None,
        total_latency_ms=1,
        canonical_person_id="550e8400-e29b-41d4-a716-446655440000",
    )
    with pytest.raises(ValueError, match="person does not exist"):
        await RuntimeTurnObservationRepository(database).record_turn(observation)
    assert await _observation_rows(database) == []


@pytest.mark.asyncio
async def test_handle_pre_admits_once_for_v1_and_v2(database: Database) -> None:
    settings = make_settings("sqlite+aiosqlite:///:memory:")
    harness = build_harness(database, settings)
    registry, _router = _wire_ingress(harness, database)
    inner = harness.processor._canonical_ingress
    assert isinstance(inner, CanonicalIngressResolver)
    counter = _CountingIngress(inner)
    harness.processor._canonical_ingress = counter
    v1 = await harness.processor.handle(
        _inbound(message_id="admit-v1", text="v1 once"),
        IngressSender(_Bot("8000")),
    )
    assert v1.handled is True
    assert counter.calls == 1
    await _flip_v2(database)
    bot = _Bot("8000")
    async with database.sessions() as session, session.begin():
        presence = await ensure_v2_presence(session, "8000")
    registry.connect(bot)
    registry.bind_presence(platform="qq", external_account_id="8000", presence_id=presence)
    v2 = await harness.processor.handle(
        _inbound(message_id="admit-v2", text="v2 once"),
        IngressSender(bot),
    )
    assert v2.handled is True
    assert counter.calls == 2


@pytest.mark.asyncio
async def test_handle_admitted_direct_still_pre_admits_once(database: Database) -> None:
    settings = make_settings("sqlite+aiosqlite:///:memory:")
    harness = build_harness(database, settings)
    registry, _router = _wire_ingress(harness, database)
    await _flip_v2(database)
    bot = _Bot("8000")
    async with database.sessions() as session, session.begin():
        presence = await ensure_v2_presence(session, "8000")
    registry.connect(bot)
    registry.bind_presence(platform="qq", external_account_id="8000", presence_id=presence)
    inner = harness.processor._canonical_ingress
    assert isinstance(inner, CanonicalIngressResolver)
    counter = _CountingIngress(inner)
    harness.processor._canonical_ingress = counter
    result = await harness.processor._handle_admitted(
        _inbound(message_id="admit-direct", text="direct"),
        IngressSender(bot),
    )
    assert result.handled is True
    assert counter.calls == 1


def test_observation_refs_omit_person_for_yuki_author() -> None:
    from qq_ai_bot.services.processor import _observation_canonical_refs

    inbound = replace(
        _inbound(message_id="yuki-obs", text="hi"),
        person_id="should-not-use",
        conversation_id="conv-1",
    )
    admitted = IngressPreAdmit(
        dropped=False,
        reason="admitted",
        message=inbound,
        yuki_account_ids=frozenset({"8000"}),
        presence_id="presence-1",
        connection_id=None,
        gateway_instance_id=None,
        person_id=None,
        space_id=None,
        space_binding_id=None,
        conversation_id="conv-1",
        primary_alias="bot:8000:private:8000",
        author_kind=AuthorKind.YUKI.value,
        author_person_id=None,
        author_presence_id="presence-1",
        provider="qq",
        handle_external_account_id="8000",
    )
    conversation_id, person_id, space_id = _observation_canonical_refs(inbound, admitted)
    assert conversation_id == "conv-1"
    assert person_id is None
    assert space_id is None


@pytest.mark.asyncio
async def test_complete_v2_same_person_bindings_share_one_relationship(
    database: Database,
) -> None:
    from qq_ai_bot.identity.dual_write import _create_person_binding
    from qq_ai_bot.persistence.models import PersonRelationshipModel
    from qq_ai_bot.persistence.repositories import RelationshipRepository

    await _flip_v2(database)
    async with database.sessions() as session, session.begin():
        created = await _create_person_binding(
            session, external_id="1001", display_name="", now=_NOW
        )
        session.add(
            IdentityBindingModel(
                id=str(uuid4()),
                person_id=created.person_id,
                platform=IDENTITY_PLATFORM,
                external_account_id="1002",
                display_name="",
                status="active",
                revision=1,
                created_at=_NOW,
                updated_at=_NOW,
            )
        )
    relationships = RelationshipRepository(database)
    first = await relationships.get_or_create("1001")
    second = await relationships.get("1002")
    assert second is not None
    assert first.user_id == "1001"
    assert second.user_id == "1002"
    assert second.affection_score == first.affection_score
    assert second.trust_score == first.trust_score
    created = await relationships.get_or_create("1002")
    assert created.affection_score == first.affection_score
    async with database.sessions() as session:
        rows = list(await session.scalars(select(PersonRelationshipModel)))
    assert len(rows) == 1
    assert await _carrier_counts(database) == (0, 0, 0)


@pytest.mark.asyncio
async def test_complete_v2_inconsistent_relationship_owners_fail_closed(
    database: Database,
) -> None:
    from qq_ai_bot.identity.dual_write import _create_person_binding
    from qq_ai_bot.persistence.models import PersonRelationshipModel
    from qq_ai_bot.persistence.repositories import RelationshipRepository

    await _flip_v2(database)
    async with database.sessions() as session, session.begin():
        created = await _create_person_binding(
            session, external_id="1001", display_name="", now=_NOW
        )
        session.add(
            IdentityBindingModel(
                id=str(uuid4()),
                person_id=created.person_id,
                platform=IDENTITY_PLATFORM,
                external_account_id="1003",
                display_name="",
                status="active",
                revision=1,
                created_at=_NOW,
                updated_at=_NOW,
            )
        )
        session.add(
            PersonRelationshipModel(
                user_id="1001",
                affection_score=50,
                trust_score=50,
                created_at=_NOW,
                updated_at=_NOW,
                canonical_person_id=created.person_id,
            )
        )
        session.add(
            PersonRelationshipModel(
                user_id="1003",
                affection_score=10,
                trust_score=10,
                created_at=_NOW,
                updated_at=_NOW,
                canonical_person_id=created.person_id,
            )
        )
    with pytest.raises(IdentityDualWriteError) as exc:
        await RelationshipRepository(database).get("1003")
    assert exc.value.category == "canonical_owner_mismatch"
    assert "1003" not in str(exc.value)


def test_complete_v2_does_not_swallow_dual_write_as_profile_success() -> None:
    import inspect

    from qq_ai_bot.services.user_profiles import UserProfileService

    source = inspect.getsource(UserProfileService.capture)
    assert "IdentityDualWriteError" not in source


def test_event_record_author_projection_and_display() -> None:
    from qq_ai_bot.persistence.repository_helpers import _event_record
    from qq_ai_bot.persistence.repository_records import EventRecord

    base = dict(
        id=1,
        bot_user_id="8001",
        platform_message_id="e1",
        scope_type=ScopeType.PRIVATE,
        sender_user_id="8000",
        direction="outbound",
        content="hi",
        visual_summary="",
        segments=(),
        occurred_at=_NOW,
    )
    yuki = EventRecord(**base, author_kind="yuki", suppression_status="keeper")
    legacy = EventRecord(**{**base, "sender_user_id": "8001"}, author_kind=None)
    external = EventRecord(**base, author_kind="external_bot")
    system = EventRecord(**base, author_kind="system")
    person = EventRecord(**{**base, "sender_user_id": "1001"}, author_kind="person")
    assert yuki.sender_display_name == "Yuki"
    assert yuki.author_is_yuki() is True
    assert yuki.author_is_human() is False
    assert legacy.sender_display_name == "Yuki"
    assert external.sender_display_name == "external bot"
    assert system.sender_display_name == "system"
    assert person.sender_display_name.startswith("QQ ")
    row = ChatEventModel(
        id=9,
        bot_user_id="8000",
        platform_message_id="proj",
        scope_type="private",
        sender_user_id="1001",
        sender_nickname="",
        sender_group_card="",
        direction="inbound",
        event_kind="message",
        content="x",
        visual_summary="",
        segments_json="[]",
        origin="user_message",
        occurred_at=_NOW,
        observed_at=_NOW,
        canonical_conversation_id="conv-1",
        canonical_event_id="evt-1",
        author_kind="person",
        author_person_id="person-1",
        author_presence_id=None,
        ingress_presence_id="pres-1",
        suppression_status="keeper",
    )
    projected = _event_record(row)
    assert projected.canonical_conversation_id == "conv-1"
    assert projected.canonical_event_id == "evt-1"
    assert projected.author_kind == "person"
    assert projected.author_person_id == "person-1"
    assert projected.ingress_presence_id == "pres-1"
    assert projected.suppression_status == "keeper"


def _spy_person_lookups(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    from qq_ai_bot.identity import canonical_projections
    from qq_ai_bot.persistence.people_repository import PeopleRepository
    from qq_ai_bot.persistence.relationship_repository import RelationshipRepository

    seen: list[str] = []
    real_members = PeopleRepository.members_in_group
    real_get = PeopleRepository.get
    real_get_many = PeopleRepository.get_many
    real_observe = PeopleRepository.observe
    real_require = canonical_projections.require_person_binding
    real_rel = RelationshipRepository.get
    real_rel_create = RelationshipRepository.get_or_create

    async def members(self: PeopleRepository, user_ids: tuple[str, ...], group_id: str):
        seen.extend(user_ids)
        return await real_members(self, user_ids, group_id)

    async def get(self: PeopleRepository, *, user_id: str, group_id: str | None = None):
        seen.append(user_id)
        return await real_get(self, user_id=user_id, group_id=group_id)

    async def get_many(
        self: PeopleRepository,
        user_ids: tuple[str, ...],
        *,
        group_id: str | None = None,
    ):
        seen.extend(user_ids)
        return await real_get_many(self, user_ids, group_id=group_id)

    async def observe(
        self: PeopleRepository,
        *,
        user_id: str,
        nickname: str,
        group_id: str | None = None,
        group_card: str = "",
        group_name: str = "",
        nickname_known: bool = True,
        group_card_known: bool = True,
        is_bot: bool = False,
        initial_affection: int | None = None,
        initial_trust: int | None = None,
    ) -> None:
        seen.append(user_id)
        await real_observe(
            self,
            user_id=user_id,
            nickname=nickname,
            group_id=group_id,
            group_card=group_card,
            group_name=group_name,
            nickname_known=nickname_known,
            group_card_known=group_card_known,
            is_bot=is_bot,
            initial_affection=initial_affection,
            initial_trust=initial_trust,
        )

    async def require(session: object, user_id: str, *, allow_disabled: bool = False):
        seen.append(user_id)
        return await real_require(session, user_id, allow_disabled=allow_disabled)

    async def rel_get(self: RelationshipRepository, user_id: str):
        seen.append(user_id)
        return await real_rel(self, user_id)

    async def rel_create(
        self: RelationshipRepository,
        user_id: str,
        *,
        initial_affection: int | None = None,
        initial_trust: int | None = None,
        session: object | None = None,
    ):
        seen.append(user_id)
        return await real_rel_create(
            self,
            user_id,
            initial_affection=initial_affection,
            initial_trust=initial_trust,
            session=session,
        )

    monkeypatch.setattr(PeopleRepository, "members_in_group", members)
    monkeypatch.setattr(PeopleRepository, "get", get)
    monkeypatch.setattr(PeopleRepository, "get_many", get_many)
    monkeypatch.setattr(PeopleRepository, "observe", observe)
    monkeypatch.setattr(canonical_projections, "require_person_binding", require)
    monkeypatch.setattr(RelationshipRepository, "get", rel_get)
    monkeypatch.setattr(RelationshipRepository, "get_or_create", rel_create)
    return seen


@pytest.mark.asyncio
async def test_complete_v2_other_presence_mention_triggers_via_overlay(
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _spy_person_lookups(monkeypatch)
    settings = make_settings("sqlite+aiosqlite:///:memory:")
    harness = build_harness(database, settings)
    registry, _router = _wire_ingress(harness, database)
    await _flip_v2(database)
    bot = _Bot("8000")
    async with database.sessions() as session, session.begin():
        presence_a = await ensure_v2_presence(session, "8000")
        await ensure_v2_presence(session, "8001")
        await ensure_v2_space(session, "2001")
    registry.connect(bot)
    registry.bind_presence(platform="qq", external_account_id="8000", presence_id=presence_a)
    inbound = _inbound(
        message_id="g-mention-other",
        group_id="2001",
        mentions_bot=False,
        mentioned_user_ids=("8001",),
        text="点一下另一号",
    )
    admitted = await harness.processor._canonical_ingress.pre_admit(bot, inbound)
    assert admitted is not None and not admitted.dropped
    assert inbound.mentions_bot is False
    assert admitted.message.mentions_bot is True
    result = await harness.processor.handle(inbound, IngressSender(bot))
    assert result.handled is True
    assert result.reason == "chat"
    assert result.sent_messages > 0
    assert "8001" not in seen
    async with database.sessions() as session:
        binding = await session.scalar(
            select(IdentityBindingModel).where(IdentityBindingModel.external_account_id == "8001")
        )
        people = await session.get(PersonModel, "8001")
    assert binding is None
    assert people is None


@pytest.mark.asyncio
async def test_complete_v2_require_mention_matrix(database: Database) -> None:
    from qq_ai_bot.identity.db_models import CanonicalSpaceModel

    settings = make_settings("sqlite+aiosqlite:///:memory:")
    harness = build_harness(database, settings)
    registry, _router = _wire_ingress(harness, database)
    await _flip_v2(database)
    bot = _Bot("8000")
    async with database.sessions() as session, session.begin():
        presence = await ensure_v2_presence(session, "8000")
        space_id = await ensure_v2_space(session, "2001")
        space = await session.get(CanonicalSpaceModel, space_id)
        assert space is not None
        space.require_mention = False
    registry.connect(bot)
    registry.bind_presence(platform="qq", external_account_id="8000", presence_id=presence)
    opened = await harness.processor.handle(
        _inbound(message_id="g-open", group_id="2001", text="路过一句"),
        IngressSender(bot),
    )
    assert opened.handled is True
    assert opened.reason == "chat"
    async with database.sessions() as session, session.begin():
        space = await session.get(CanonicalSpaceModel, space_id)
        assert space is not None
        space.require_mention = True
    required = await harness.processor.handle(
        _inbound(message_id="g-required", group_id="2001", text="还是路过"),
        IngressSender(bot),
    )
    assert required.handled is False
    assert required.reason == "group_observed"
    self_msg = await harness.processor.handle(
        _inbound(message_id="g-self", group_id="2001", user_id="8000", text="自己"),
        IngressSender(bot),
    )
    assert self_msg.handled is False
    assert self_msg.reason == "bot_message"
    async with database.sessions() as session, session.begin():
        space = await session.get(CanonicalSpaceModel, space_id)
        assert space is not None
        space.enabled = False
        space.require_mention = False
    disabled = await harness.processor.handle(
        _inbound(message_id="g-disabled", group_id="2001", text="关群"),
        IngressSender(bot),
    )
    assert disabled.handled is False
    assert disabled.reason == "group_disabled"


@pytest.mark.asyncio
async def test_complete_v2_canonical_reply_and_spoof(
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _spy_person_lookups(monkeypatch)
    settings = make_settings("sqlite+aiosqlite:///:memory:")
    harness = build_harness(database, settings)
    registry, _router = _wire_ingress(harness, database)
    await _flip_v2(database)
    bot = _Bot("8000")
    async with database.sessions() as session, session.begin():
        presence = await ensure_v2_presence(session, "8000")
        await ensure_v2_presence(session, "8001")
        await ensure_v2_space(session, "2001")
    registry.connect(bot)
    registry.bind_presence(platform="qq", external_account_id="8000", presence_id=presence)
    first = await harness.processor.handle(
        _inbound(message_id="g-reply-base", group_id="2001", mentions_bot=True, text="先说话"),
        IngressSender(bot),
    )
    assert first.handled is True
    async with database.sessions() as session:
        outbound = await session.scalar(
            select(ChatEventModel).where(ChatEventModel.direction == "outbound")
        )
        assert outbound is not None
        conversation_id = outbound.canonical_conversation_id
        yuki_id = outbound.platform_message_id
    missing_sender = await harness.processor.handle(
        _inbound(
            message_id="g-reply-yuki",
            group_id="2001",
            text="回Yuki",
            reply_to_message_id=yuki_id,
        ),
        IngressSender(bot),
    )
    assert missing_sender.handled is True
    assert missing_sender.reason == "chat"
    async with database.sessions() as session, session.begin():
        session.add(
            ChatEventModel(
                bot_user_id="8000",
                platform_message_id="ext-bot-msg",
                scope_type="group",
                group_id="2001",
                sender_user_id="7777",
                sender_nickname="",
                sender_group_card="",
                direction="inbound",
                event_kind="message",
                content="third",
                visual_summary="",
                segments_json="[]",
                origin="user_message",
                occurred_at=_NOW,
                observed_at=_NOW,
                canonical_event_id=str(uuid4()),
                canonical_conversation_id=conversation_id,
                author_kind="external_bot",
                suppression_status="keeper",
            )
        )
    spoofed = await harness.processor.handle(
        _inbound(
            message_id="g-reply-spoof",
            group_id="2001",
            text="伪造成功?",
            reply_to_message_id="ext-bot-msg",
            reply_sender_user_id="8000",
        ),
        IngressSender(bot),
    )
    assert spoofed.handled is False
    assert spoofed.reason == "group_observed"
    fallback = await harness.processor.handle(
        _inbound(
            message_id="g-reply-fallback",
            group_id="2001",
            text="回旧号",
            reply_to_message_id="no-such-msg",
            reply_sender_user_id="8001",
        ),
        IngressSender(bot),
    )
    assert fallback.handled is True
    assert fallback.reason == "chat"
    assert "8001" not in seen
    assert "7777" not in seen
    async with database.sessions() as session:
        for external in ("8001", "7777"):
            binding = await session.scalar(
                select(IdentityBindingModel).where(
                    IdentityBindingModel.external_account_id == external
                )
            )
            people = await session.get(PersonModel, external)
            assert binding is None
            assert people is None


@pytest.mark.asyncio
async def test_complete_v2_external_bot_mention_skips_person_lookup(
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _spy_person_lookups(monkeypatch)
    settings = make_settings("sqlite+aiosqlite:///:memory:")
    harness = build_harness(database, settings)
    registry, _router = _wire_ingress(harness, database)
    await _flip_v2(database)
    bot = _Bot("8000")
    async with database.sessions() as session, session.begin():
        presence = await ensure_v2_presence(session, "8000")
        await ensure_v2_space(session, "2001")
    registry.connect(bot)
    registry.bind_presence(platform="qq", external_account_id="8000", presence_id=presence)
    result = await harness.processor.handle(
        _inbound(
            message_id="g-mention-bot",
            group_id="2001",
            mentions_bot=True,
            mentioned_user_ids=("7777",),
            text="看那个机器人",
            is_bot=False,
        ),
        IngressSender(bot),
    )
    assert result.handled is True
    assert "7777" not in seen
    async with database.sessions() as session:
        binding = await session.scalar(
            select(IdentityBindingModel).where(IdentityBindingModel.external_account_id == "7777")
        )
        people = await session.get(PersonModel, "7777")
    assert binding is None
    assert people is None


@pytest.mark.asyncio
async def test_complete_v2_system_reply_skips_person_lookup(
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _spy_person_lookups(monkeypatch)
    settings = make_settings("sqlite+aiosqlite:///:memory:")
    harness = build_harness(database, settings)
    registry, _router = _wire_ingress(harness, database)
    await _flip_v2(database)
    bot = _Bot("8000")
    async with database.sessions() as session, session.begin():
        presence = await ensure_v2_presence(session, "8000")
        await ensure_v2_space(session, "2001")
        session.add(
            ChatEventModel(
                bot_user_id="8000",
                platform_message_id="sys-event",
                scope_type="group",
                group_id="2001",
                sender_user_id="0",
                sender_nickname="",
                sender_group_card="",
                direction="inbound",
                event_kind="message",
                content="system notice",
                visual_summary="",
                segments_json="[]",
                origin="user_message",
                occurred_at=_NOW,
                observed_at=_NOW,
                canonical_event_id=str(uuid4()),
                author_kind="system",
                suppression_status="keeper",
            )
        )
    registry.connect(bot)
    registry.bind_presence(platform="qq", external_account_id="8000", presence_id=presence)
    result = await harness.processor.handle(
        _inbound(
            message_id="g-reply-system",
            group_id="2001",
            mentions_bot=True,
            text="回系统",
            reply_to_message_id="sys-event",
            reply_sender_user_id="0",
        ),
        IngressSender(bot),
    )
    assert result.handled is True
    assert "0" not in seen
    async with database.sessions() as session:
        binding = await session.scalar(
            select(IdentityBindingModel).where(IdentityBindingModel.external_account_id == "0")
        )
        people = await session.get(PersonModel, "0")
    assert binding is None
    assert people is None


@pytest.mark.asyncio
async def test_complete_v2_secondary_binding_is_one_person_profile_target(
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from qq_ai_bot.identity.dual_write import _create_person_binding
    from qq_ai_bot.persistence.people_repository import PeopleRepository

    seen = _spy_person_lookups(monkeypatch)
    settings = make_settings("sqlite+aiosqlite:///:memory:")
    harness = build_harness(database, settings)
    registry, _router = _wire_ingress(harness, database)
    await _flip_v2(database)
    bot = _Bot("8000")
    async with database.sessions() as session, session.begin():
        presence = await ensure_v2_presence(session, "8000")
        created = await _create_person_binding(
            session, external_id="1001", display_name="甲", now=_NOW
        )
        session.add(
            IdentityBindingModel(
                id=str(uuid4()),
                person_id=created.person_id,
                platform=IDENTITY_PLATFORM,
                external_account_id="1002",
                display_name="乙",
                status="active",
                revision=1,
                created_at=_NOW,
                updated_at=_NOW,
            )
        )
        await ensure_v2_space(session, "2001")
        person_id = created.person_id
    registry.connect(bot)
    registry.bind_presence(platform="qq", external_account_id="8000", presence_id=presence)
    await PeopleRepository(database).observe(
        user_id="1002",
        nickname="乙",
        group_id="2001",
        group_card="乙",
    )
    result = await harness.processor.handle(
        _inbound(
            message_id="g-two-bindings",
            group_id="2001",
            mentions_bot=True,
            mentioned_user_ids=("1001", "1002"),
            text="他们是同一个人",
        ),
        IngressSender(bot),
    )
    assert result.handled is True
    assert "8001" not in seen
    request = harness.provider.requests[0]  # type: ignore[attr-defined]
    envelope = next(
        item.content or ""
        for item in request.messages
        if '"id":"context.people_and_scene"' in (item.content or "")
    )
    envelope_items, _ = json.JSONDecoder().raw_decode(envelope[envelope.index("[") :])
    context_item = next(item for item in envelope_items if item["id"] == "context.people_and_scene")
    payload_items = {item["id"]: item["data"] for item in context_item["data"]["items"]}
    subjects = payload_items["available_memory_subjects"]
    refs = [item["subject_ref"] for item in subjects]
    assert refs.count("mentioned_user_1") == 1
    assert "mentioned_user_2" not in refs
    async with database.sessions() as session:
        person_count = await session.scalar(select(func.count()).select_from(CanonicalPersonModel))
        persons = int(person_count or 0)
        bindings = list(
            await session.scalars(
                select(IdentityBindingModel).where(IdentityBindingModel.person_id == person_id)
            )
        )
    assert persons == 1
    assert {row.external_account_id for row in bindings} == {"1001", "1002"}


@pytest.mark.asyncio
async def test_complete_v2_cadence_and_memory_job_without_people_row(
    database: Database,
) -> None:
    from qq_ai_bot.conversation.features import AdmissionFeatureBuilder
    from qq_ai_bot.memory.enums import MemoryJobStatus
    from qq_ai_bot.memory.repository import MemoryJobRepository
    from qq_ai_bot.persistence.models import MemoryJobModel
    from qq_ai_bot.persistence.repositories import RelationshipRepository

    settings = make_settings("sqlite+aiosqlite:///:memory:")
    harness = build_harness(database, settings)
    registry, _router = _wire_ingress(harness, database)
    await _flip_v2(database)
    bot_a = _Bot("8000")
    bot_b = _Bot("8001")
    async with database.sessions() as session, session.begin():
        presence_a = await ensure_v2_presence(session, "8000")
        presence_b = await ensure_v2_presence(session, "8001")
    registry.connect(bot_a)
    registry.bind_presence(platform="qq", external_account_id="8000", presence_id=presence_a)
    first = await harness.processor.handle(
        _inbound(message_id="p-cadence-1", text="第一句历史"),
        IngressSender(bot_a),
    )
    assert first.handled is True
    registry.connect(bot_b)
    registry.bind_presence(platform="qq", external_account_id="8001", presence_id=presence_b)
    second = await harness.processor.handle(
        _inbound(message_id="p-cadence-2", bot_user_id="8001", text="第二句历史"),
        IngressSender(bot_b),
    )
    assert second.handled is True
    assert await _carrier_counts(database) == (0, 0, 0)
    async with database.sessions() as session:
        inbound_rows = list(
            await session.scalars(
                select(ChatEventModel).where(ChatEventModel.direction == "inbound")
            )
        )
        jobs = list(await session.scalars(select(MemoryJobModel)))
        conversation_id = inbound_rows[0].canonical_conversation_id
    assert conversation_id
    assert len(jobs) >= 1
    assert {row.status for row in jobs} <= {
        MemoryJobStatus.PENDING.value,
        MemoryJobStatus.PROCESSING.value,
        MemoryJobStatus.DONE.value,
    }
    recent = await harness.ledger.list_canonical_recent(conversation_id, limit=20)
    bots = {row.bot_user_id for row in recent}
    assert "8000" in bots and "8001" in bots
    assert any(row.author_is_yuki() for row in recent)
    assert all(not row.author_is_human() or row.author_kind == "person" for row in recent)
    inbound = _inbound(message_id="p-cadence-3", bot_user_id="8001", text="第三句")
    admitted = await harness.processor._canonical_ingress.pre_admit(bot_b, inbound)
    assert admitted is not None and not admitted.dropped
    features = await AdmissionFeatureBuilder(
        ledger=harness.ledger,
        relationships=RelationshipRepository(database),
    ).admission_features(
        inbound=admitted.message,
        content="第三句",
        runtime=await harness.processor._runtime_config.snapshot(user_id="1001"),
    )
    assert features.continuation is True
    assert features.recent_bot_messages >= 2
    assert features.recent_total_messages >= 4
    jobs_repo = MemoryJobRepository(database)
    skip_ids: list[int] = []
    async with database.sessions() as session, session.begin():
        kinds = (
            ("yuki", "in-yuki"),
            ("external_bot", "in-ext"),
            ("system", "in-sys"),
        )
        for kind, platform_id in kinds:
            row = ChatEventModel(
                bot_user_id="8001",
                platform_message_id=platform_id,
                scope_type="private",
                private_peer_user_id="1001",
                sender_user_id="8001" if kind != "external_bot" else "7777",
                sender_nickname="",
                sender_group_card="",
                direction="inbound",
                event_kind="message",
                content=kind,
                visual_summary="",
                segments_json="[]",
                origin="user_message",
                occurred_at=_NOW,
                observed_at=_NOW,
                canonical_event_id=str(uuid4()),
                canonical_conversation_id=conversation_id,
                author_kind=kind,
                suppression_status="keeper",
            )
            session.add(row)
            await session.flush()
            skip_ids.append(row.id)
    assert skip_ids
    for event_id in skip_ids:
        assert await jobs_repo.enqueue(event_id, "private:1001") is False


@pytest.mark.asyncio
async def test_complete_v2_canonical_recent_excludes_duplicate_and_suppressed(
    database: Database,
) -> None:
    from qq_ai_bot.conversation.features import AdmissionFeatureBuilder
    from qq_ai_bot.persistence.repositories import RelationshipRepository

    settings = make_settings("sqlite+aiosqlite:///:memory:")
    harness = build_harness(database, settings)
    registry, _router = _wire_ingress(harness, database)
    await _flip_v2(database)
    bot = _Bot("8000")
    async with database.sessions() as session, session.begin():
        presence = await ensure_v2_presence(session, "8000")
    registry.connect(bot)
    registry.bind_presence(platform="qq", external_account_id="8000", presence_id=presence)
    first = await harness.processor.handle(
        _inbound(message_id="hist-1", text="先说话"),
        IngressSender(bot),
    )
    assert first.handled is True
    async with database.sessions() as session:
        inbound_row = await session.scalar(
            select(ChatEventModel).where(ChatEventModel.direction == "inbound")
        )
        yuki_row = await session.scalar(
            select(ChatEventModel).where(ChatEventModel.author_kind == "yuki")
        )
    assert inbound_row is not None and inbound_row.canonical_conversation_id
    assert yuki_row is not None
    conversation_id = inbound_row.canonical_conversation_id

    async def _features() -> object:
        admitted = await harness.processor._canonical_ingress.pre_admit(
            bot,
            _inbound(message_id=f"probe-{uuid4()}", text="probe"),
        )
        assert admitted is not None and not admitted.dropped
        return await AdmissionFeatureBuilder(
            ledger=harness.ledger,
            relationships=RelationshipRepository(database),
        ).admission_features(
            inbound=admitted.message,
            content="probe",
            runtime=await harness.processor._runtime_config.snapshot(user_id="1001"),
        )

    async def _insert(
        *,
        platform_id: str,
        author_kind: str | None,
        sender_user_id: str,
        direction: str,
        suppression_status: str | None,
        canonical_event_id: str | None = None,
        utterance_fingerprint: str | None = None,
    ) -> None:
        async with database.sessions() as session, session.begin():
            session.add(
                ChatEventModel(
                    bot_user_id="8000",
                    platform_message_id=platform_id,
                    scope_type="private",
                    private_peer_user_id="1001",
                    sender_user_id=sender_user_id,
                    sender_nickname="",
                    sender_group_card="",
                    direction=direction,
                    event_kind="message",
                    content=platform_id,
                    visual_summary="",
                    segments_json="[]",
                    origin="user_message",
                    occurred_at=_NOW,
                    observed_at=_NOW,
                    canonical_event_id=canonical_event_id or str(uuid4()),
                    canonical_conversation_id=conversation_id,
                    author_kind=author_kind,
                    author_presence_id=yuki_row.author_presence_id
                    if author_kind == "yuki"
                    else None,
                    utterance_fingerprint=utterance_fingerprint,
                    suppression_status=suppression_status,
                )
            )

    await _insert(
        platform_id="hist-human-keeper",
        author_kind="person",
        sender_user_id="1001",
        direction="inbound",
        suppression_status="keeper",
    )
    after_human = await _features()
    assert after_human.continuation is False
    assert after_human.pending_message_count == 1
    human_recent = await harness.ledger.list_canonical_recent(conversation_id, limit=20)
    human_ids = {row.id for row in human_recent}

    await _insert(
        platform_id="hist-dup-yuki",
        author_kind="yuki",
        sender_user_id="8000",
        direction="outbound",
        suppression_status="duplicate",
        canonical_event_id=yuki_row.canonical_event_id,
        utterance_fingerprint=yuki_row.utterance_fingerprint or "dup-token",
    )
    await _insert(
        platform_id="hist-sup-yuki",
        author_kind="yuki",
        sender_user_id="8000",
        direction="outbound",
        suppression_status="suppressed",
    )
    await _insert(
        platform_id="hist-unknown-yuki",
        author_kind="yuki",
        sender_user_id="8000",
        direction="outbound",
        suppression_status="shadow",
    )
    poisoned = await _features()
    assert poisoned.continuation is False
    assert poisoned.pending_message_count == 1
    poisoned_recent = await harness.ledger.list_canonical_recent(conversation_id, limit=20)
    assert {row.id for row in poisoned_recent} == human_ids
    assert {row.suppression_status for row in poisoned_recent} <= {None, "keeper"}

    await _insert(
        platform_id="hist-yuki-keeper",
        author_kind="yuki",
        sender_user_id="8000",
        direction="outbound",
        suppression_status="keeper",
    )
    after_yuki = await _features()
    assert after_yuki.continuation is True
    assert after_yuki.pending_message_count == 0

    await _insert(
        platform_id="hist-sup-human",
        author_kind="person",
        sender_user_id="1001",
        direction="inbound",
        suppression_status="suppressed",
    )
    after_suppressed_human = await _features()
    assert after_suppressed_human.continuation is True
    assert after_suppressed_human.pending_message_count == 0

    await _insert(
        platform_id="hist-null-human",
        author_kind=None,
        sender_user_id="1001",
        direction="inbound",
        suppression_status=None,
    )
    after_legacy = await _features()
    assert after_legacy.continuation is False
    assert after_legacy.pending_message_count == 1
    latest = await harness.ledger.list_canonical_recent(conversation_id, limit=20)
    assert any(row.author_kind is None and row.sender_user_id == "1001" for row in latest)
    assert all(row.suppression_status in {None, "keeper"} for row in latest)


async def _assemble_v2_prompt(
    harness: object,
    database: Database,
    inbound: InboundMessage,
) -> AssembledContext:
    from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel
    from qq_ai_bot.conversation.hydrate import hydrate_scope_state_from_canonical
    from qq_ai_bot.conversation.scope import ConversationTurnSnapshot
    from qq_ai_bot.domain.profiles import UserProfileSnapshot
    from qq_ai_bot.persistence.repositories import PeopleRepository

    identity = inbound.scope()
    async with database.sessions() as session:
        row = await session.scalar(
            select(ChatEventModel).where(ChatEventModel.platform_message_id == inbound.message_id)
        )
        assert row is not None and row.canonical_conversation_id
        conversation = await session.get(CanonicalConversationModel, row.canonical_conversation_id)
        assert conversation is not None
        state = await hydrate_scope_state_from_canonical(session, identity, conversation)
    people = PeopleRepository(database)
    profile = await people.get(user_id=inbound.sender.user_id) or UserProfileSnapshot(
        user_id=inbound.sender.user_id,
        scope_type=inbound.scope_type,
        nickname=inbound.sender.nickname,
    )
    runtime_key = state.runtime_scope_key or identity.key
    turn = ConversationTurnSnapshot(
        scope_id=state.id,
        scope_key=runtime_key,
        generation=state.generation,
        trigger_event_id=row.id,
        coordinator_version=1,
        transport_scope_key=identity.key if identity.key != runtime_key else None,
    )
    runtime = await harness.processor._runtime_config.snapshot(user_id=inbound.sender.user_id)
    return await harness.processor._chat._context_assembler.assemble(
        inbound=inbound,
        identity=identity,
        profile=profile,
        turn=turn,
        content=inbound.text,
        runtime=runtime,
        persist_memory_exposure=False,
    )


def _prompt_fingerprint(context: AssembledContext) -> tuple[object, ...]:
    history = tuple((message.role, message.content) for message in context.history_messages)
    current = (context.current_message.role, context.current_message.content)
    delivery = tuple(item.get("platform_message_id") for item in context.recent_delivery)
    return (history, current, delivery, frozenset(context.visible_event_ids))


@pytest.mark.asyncio
async def test_complete_v2_assembler_prompt_snapshot_uses_keeper_whitelist(
    database: Database,
) -> None:
    settings = make_settings("sqlite+aiosqlite:///:memory:")
    provider = FakeLLMProvider()
    harness = build_harness(database, settings, provider)
    registry, _router = _wire_ingress(harness, database)
    await _flip_v2(database)
    bot = _Bot("8000")
    async with database.sessions() as session, session.begin():
        presence = await ensure_v2_presence(session, "8000")
    registry.connect(bot)
    registry.bind_presence(platform="qq", external_account_id="8000", presence_id=presence)
    sender = IngressSender(bot)
    first = await harness.processor.handle(
        _inbound(message_id="prompt-hist-1", text="先说话"),
        sender,
    )
    assert first.handled is True
    async with database.sessions() as session:
        inbound_row = await session.scalar(
            select(ChatEventModel).where(ChatEventModel.platform_message_id == "prompt-hist-1")
        )
        yuki_row = await session.scalar(
            select(ChatEventModel).where(ChatEventModel.author_kind == "yuki")
        )
    assert inbound_row is not None and inbound_row.canonical_conversation_id
    assert yuki_row is not None
    conversation_id = inbound_row.canonical_conversation_id

    async def _insert(
        *,
        platform_id: str,
        content: str,
        author_kind: str | None,
        sender_user_id: str,
        direction: str,
        suppression_status: str | None,
        canonical_event_id: str | None = None,
        utterance_fingerprint: str | None = None,
    ) -> int:
        async with database.sessions() as session, session.begin():
            row = ChatEventModel(
                bot_user_id="8000",
                platform_message_id=platform_id,
                scope_type="private",
                private_peer_user_id="1001",
                sender_user_id=sender_user_id,
                sender_nickname="",
                sender_group_card="",
                direction=direction,
                event_kind="message",
                content=content,
                visual_summary="",
                segments_json="[]",
                origin="user_message",
                occurred_at=_NOW,
                observed_at=_NOW,
                canonical_event_id=canonical_event_id or str(uuid4()),
                canonical_conversation_id=conversation_id,
                author_kind=author_kind,
                author_presence_id=yuki_row.author_presence_id if author_kind == "yuki" else None,
                utterance_fingerprint=utterance_fingerprint,
                suppression_status=suppression_status,
            )
            session.add(row)
            await session.flush()
            return int(row.id)

    await _insert(
        platform_id="prompt-human-keeper",
        content="PROMPT-KEEPER-HUMAN",
        author_kind="person",
        sender_user_id="1001",
        direction="inbound",
        suppression_status="keeper",
    )
    probe_a = _inbound(message_id="prompt-probe-a", text="probe-a")
    handled_a = await harness.processor.handle(probe_a, sender)
    assert handled_a.handled is True
    ctx_a = await _assemble_v2_prompt(harness, database, probe_a)
    hist_a, current_a, _delivery_a, visible_a = _prompt_fingerprint(ctx_a)
    assert any("PROMPT-KEEPER-HUMAN" in str(item[1]) for item in hist_a)

    poison_ids = {
        await _insert(
            platform_id="prompt-dup-yuki",
            content="PROMPT-DUP-YUKI",
            author_kind="yuki",
            sender_user_id="8000",
            direction="outbound",
            suppression_status="duplicate",
            canonical_event_id=yuki_row.canonical_event_id,
            utterance_fingerprint=yuki_row.utterance_fingerprint or "dup-token",
        ),
        await _insert(
            platform_id="prompt-sup-yuki",
            content="PROMPT-SUP-YUKI",
            author_kind="yuki",
            sender_user_id="8000",
            direction="outbound",
            suppression_status="suppressed",
        ),
        await _insert(
            platform_id="prompt-unknown-yuki",
            content="PROMPT-UNKNOWN-YUKI",
            author_kind="yuki",
            sender_user_id="8000",
            direction="outbound",
            suppression_status="shadow",
        ),
    }
    poison_marks = ("PROMPT-DUP-YUKI", "PROMPT-SUP-YUKI", "PROMPT-UNKNOWN-YUKI")
    poison_platforms = ("prompt-dup-yuki", "prompt-sup-yuki", "prompt-unknown-yuki")
    probe_b = _inbound(message_id="prompt-probe-b", text="probe-b")
    handled_b = await harness.processor.handle(probe_b, sender)
    assert handled_b.handled is True
    ctx_b = await _assemble_v2_prompt(harness, database, probe_b)
    hist_b, current_b, delivery_b, visible_b = _prompt_fingerprint(ctx_b)
    assert hist_b[: len(hist_a)] == hist_a
    assert hist_b[len(hist_a)] == current_a
    assert current_a == (ctx_a.current_message.role, ctx_a.current_message.content)
    assert current_b[1] != current_a[1]
    dumped_b = "\n".join(str(item[1]) for item in hist_b)
    assert all(mark not in dumped_b for mark in poison_marks)
    assert all(mark not in str(current_b[1]) for mark in poison_marks)
    assert all(platform not in delivery_b for platform in poison_platforms)
    assert poison_ids.isdisjoint(visible_b)
    async with database.sessions() as session:
        probe_a_row = await session.scalar(
            select(ChatEventModel).where(ChatEventModel.platform_message_id == "prompt-probe-a")
        )
        probe_b_row = await session.scalar(
            select(ChatEventModel).where(ChatEventModel.platform_message_id == "prompt-probe-b")
        )
    assert probe_a_row is not None and probe_b_row is not None
    snapshot_b = await harness.processor._chat._context_assembler._rollups.load_prompt_snapshot(
        probe_b.scope(),
        before_event_id=probe_b_row.id,
    )
    assert poison_ids.isdisjoint({event.id for event in snapshot_b.raw_events})
    assert visible_a <= visible_b
    assert probe_a_row.id in visible_b

    keeper_yuki_id = await _insert(
        platform_id="prompt-yuki-keeper",
        content="PROMPT-KEEPER-YUKI",
        author_kind="yuki",
        sender_user_id="8000",
        direction="outbound",
        suppression_status="keeper",
    )
    probe_c = _inbound(message_id="prompt-probe-c", text="probe-c")
    handled_c = await harness.processor.handle(probe_c, sender)
    assert handled_c.handled is True
    ctx_c = await _assemble_v2_prompt(harness, database, probe_c)
    hist_c, _current_c, delivery_c, visible_c = _prompt_fingerprint(ctx_c)
    dumped_c = "\n".join(str(item[1]) for item in hist_c)
    assert "PROMPT-KEEPER-YUKI" in dumped_c or "prompt-yuki-keeper" in delivery_c
    assert keeper_yuki_id in visible_c
    assert poison_ids.isdisjoint(visible_c)

    null_human_id = await _insert(
        platform_id="prompt-null-human",
        content="PROMPT-NULL-LEGACY",
        author_kind=None,
        sender_user_id="1001",
        direction="inbound",
        suppression_status=None,
    )
    probe_d = _inbound(message_id="prompt-probe-d", text="probe-d")
    handled_d = await harness.processor.handle(probe_d, sender)
    assert handled_d.handled is True
    ctx_d = await _assemble_v2_prompt(harness, database, probe_d)
    hist_d, _current_d, delivery_d, visible_d = _prompt_fingerprint(ctx_d)
    dumped_d = "\n".join(str(item[1]) for item in hist_d)
    assert "PROMPT-NULL-LEGACY" in dumped_d
    assert null_human_id in visible_d
    assert poison_ids.isdisjoint(visible_d)
    assert all(platform not in delivery_d for platform in poison_platforms)
    assert all(mark not in dumped_d for mark in poison_marks)


def _runtime_time_from_request(request: object) -> dict[str, object]:
    decoder = json.JSONDecoder()
    for message in getattr(request, "messages", ()):
        content = getattr(message, "content", None) or ""
        if '"id":"runtime.time"' not in content:
            continue
        start = content.find("[")
        if start < 0:
            continue
        items, _ = decoder.raw_decode(content[start:])
        for item in items:
            if item.get("id") == "runtime.time":
                payload = item.get("data")
                if isinstance(payload, dict):
                    return payload
    raise AssertionError("runtime.time missing")


@pytest.mark.asyncio
async def test_complete_v2_binding_b_observes_person_time_speech_and_config(
    database: Database,
) -> None:
    from sqlalchemy import func, select

    from qq_ai_bot.automation.models import TurnOrigin
    from qq_ai_bot.identity.dual_write import _create_person_binding
    from qq_ai_bot.persistence.models import (
        PersonModel,
        PersonTimeSettingModel,
        RuntimeConfigOverrideModel,
    )
    from qq_ai_bot.speech.db_models import PersonSpeechPreferenceModel
    from qq_ai_bot.speech.models import VoicePreferenceMode
    from qq_ai_bot.speech.preference_repository import VoicePreferenceRepository
    from qq_ai_bot.speech.preference_service import VoicePreferenceService
    from qq_ai_bot.time.service import TimeContextService

    settings = make_settings("sqlite+aiosqlite:///:memory:")
    provider = FakeLLMProvider()
    harness = build_harness(database, settings, provider)
    registry, _router = _wire_ingress(harness, database)
    await _flip_v2(database)
    bot = _Bot("8000")
    async with database.sessions() as session, session.begin():
        presence = await ensure_v2_presence(session, "8000")
        created = await _create_person_binding(
            session, external_id="1001", display_name="", now=_NOW
        )
        session.add(
            IdentityBindingModel(
                id=str(uuid4()),
                person_id=created.person_id,
                platform=IDENTITY_PLATFORM,
                external_account_id="1002",
                display_name="",
                status="active",
                revision=1,
                created_at=_NOW,
                updated_at=_NOW,
            )
        )
    registry.connect(bot)
    registry.bind_presence(platform="qq", external_account_id="8000", presence_id=presence)

    voice = VoicePreferenceService(VoicePreferenceRepository(database))
    harness.processor._voice_preferences = voice
    harness.processor._chat._voice_preferences = voice
    time_service = TimeContextService(database)
    harness.processor._chat._time = time_service
    harness.processor._chat._context_assembler._time = time_service

    await time_service.set_timezone("1001", "America/New_York")
    saved_voice = await voice.set_persistent(
        user_id="1001",
        mode=VoicePreferenceMode.TEXT_ONLY,
        source_message_id="pref-a",
        origin=TurnOrigin.USER_MESSAGE,
    )
    assert saved_voice is not None
    written = await harness.processor._runtime_config.set_override(
        "context.local_event_limit",
        77,
        scope_type="user",
        scope_id="1001",
        actor_user_id="9000",
        trigger_message_id="cfg-a",
    )
    assert written.success

    sender = IngressSender(bot)
    result = await harness.processor.handle(
        _inbound(message_id="pref-b", user_id="1002", text="第二号账号来了"),
        sender,
    )
    assert result.handled is True
    assert provider.requests
    time_payload = _runtime_time_from_request(provider.requests[-1])
    assert time_payload["timezone"] == "America/New_York"
    assert str(time_payload["local"]).endswith("-04:00") or str(time_payload["local"]).endswith(
        "-05:00"
    )
    assert await voice.current_mode("1002") is VoicePreferenceMode.TEXT_ONLY
    snapshot = await harness.processor._runtime_config.snapshot(user_id="1002")
    assert snapshot.context.local_event_limit == 77
    dumped = json.dumps(time_payload, ensure_ascii=False)
    assert "1001" not in dumped
    assert "1002" not in dumped

    async with database.sessions() as session:
        people = int(await session.scalar(select(func.count()).select_from(PersonModel)) or 0)
        time_rows = list(await session.scalars(select(PersonTimeSettingModel)))
        speech_rows = list(await session.scalars(select(PersonSpeechPreferenceModel)))
        user_overrides = [
            row
            for row in await session.scalars(select(RuntimeConfigOverrideModel))
            if row.scope_type == "user"
        ]
    assert people == 0
    assert len(time_rows) == 1
    assert len(speech_rows) == 1
    assert len(user_overrides) == 1
