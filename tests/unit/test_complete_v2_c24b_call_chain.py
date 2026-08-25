"""C24b-2b live call-chain: chat/speech/web/MCP/plugin pass trusted Conversation ids."""

from __future__ import annotations

import json
from dataclasses import asdict, fields, replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest
from sqlalchemy import select
from tests.conftest import build_harness, make_settings
from tests.fakes import FakeWebSearchProvider

from qq_ai_bot.admin.config_service import RuntimeConfigService
from qq_ai_bot.admin.models import SpeechRuntimeConfig
from qq_ai_bot.automation.models import TurnOrigin
from qq_ai_bot.conversation.cadence import ReplyEffectRepository
from qq_ai_bot.conversation.canonical_db_models import (
    CanonicalConversationModel,
    ConversationLegacyAliasModel,
)
from qq_ai_bot.conversation.db_models import ReplyEffectEventModel
from qq_ai_bot.conversation.delivery import ReplyControlState, default_reply_spec
from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.domain.messages import ChatMessage, ChatRequest, InboundMessage, SenderIdentity
from qq_ai_bot.identity.c24_conversation import CANONICAL_KIND_MISMATCH
from qq_ai_bot.identity.db_models import IdentityRuntimeStateModel
from qq_ai_bot.identity.dual_write import (
    ensure_canonical_person_preconfig,
    ensure_canonical_presence_preconfig,
)
from qq_ai_bot.identity.errors import IdentityDualWriteError
from qq_ai_bot.llm.fake import FakeLLMProvider
from qq_ai_bot.mcp.repository import MCPRepository
from qq_ai_bot.memory.repository import MemoryFactRepository
from qq_ai_bot.memory.service import MemoryFactService
from qq_ai_bot.model_runtime.db_models import ModelInvocationModel
from qq_ai_bot.model_runtime.executor import TaskModelExecutor
from qq_ai_bot.model_runtime.models import (
    ModelCapability,
    ModelProfile,
    ModelProtocol,
    ModelRoute,
    ModelTask,
    StructuredOutputMode,
)
from qq_ai_bot.model_runtime.pool import ModelClientPool
from qq_ai_bot.model_runtime.profiles import ModelProfileCatalog
from qq_ai_bot.model_runtime.repository import ModelInvocationRepository
from qq_ai_bot.model_runtime.routes import ModelRouter
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.models import ToolInvocationModel, WebSearchRunModel
from qq_ai_bot.persistence.repositories import (
    AgentActionRepository,
    EventLedgerRepository,
    WebSearchSourceRepository,
)
from qq_ai_bot.plugin_host.facades import (
    HostPluginContext,
    PluginFacadeServices,
    PluginInvocation,
)
from qq_ai_bot.plugin_host.repository import PluginInstallationRepository
from qq_ai_bot.plugin_host.session_facade import BoundAgentSessionFacade
from qq_ai_bot.plugin_host.session_repository import PluginAgentSessionRepository
from qq_ai_bot.services.agent_runner import AgentRunner
from qq_ai_bot.services.agent_tools import AgentToolService, ToolRuntime
from qq_ai_bot.services.chat import ChatService
from qq_ai_bot.services.concurrency import ConcurrencyManager
from qq_ai_bot.services.plugin_sessions import PluginAgentSessionService, PluginSessionAuthority
from qq_ai_bot.speech.cache import SpeechCache
from qq_ai_bot.speech.db_models import SpeechGenerationModel
from qq_ai_bot.speech.models import VoiceMode
from qq_ai_bot.speech.paths import SpeechPathPolicy
from qq_ai_bot.speech.provider import (
    SpeechProviderHealth,
    SpeechSynthesisRequest,
    SynthesizedSpeech,
)
from qq_ai_bot.speech.reply_effect import VoiceReplyEffectService
from qq_ai_bot.speech.repository import SpeechGenerationRepository, VoiceProfileRepository
from qq_ai_bot.speech.service import SpeechService
from qq_ai_bot.web.models import WebMode, WebSearchResponse, WebSearchSource
from yuki_plugin_sdk.permissions import PluginPermission
from yuki_plugin_sdk.sessions import (
    CreateAgentSessionRequest,
    RunAgentSessionRequest,
    SessionContextProfile,
    SessionPersistence,
)

_NOW = datetime(2026, 8, 25, tzinfo=UTC)
_CUTOVER = "550e8400-e29b-41d4-a716-4466554400c4"


class _FailingProvider:
    async def complete(self, request: ChatRequest) -> Any:
        del request
        raise RuntimeError("synthetic-provider-failure")

    async def close(self) -> None:
        return None


class _CapturingExecutor:
    def __init__(self, content: str = "ok") -> None:
        self.conversation_ids: list[str | None] = []
        self.requests: list[ChatRequest] = []
        self.content = content

    async def execute(
        self,
        task: ModelTask,
        request: ChatRequest,
        *,
        priority: object = None,
        canonical_conversation_id: str | None = None,
    ) -> Any:
        del task, priority
        self.conversation_ids.append(canonical_conversation_id)
        self.requests.append(request)
        from qq_ai_bot.domain.messages import ChatResponse

        return ChatResponse(content=self.content, latency_seconds=0.01)

    def model_name(self, task: ModelTask) -> str:
        del task
        return "fake"

    def structured_output_mode(self, task: ModelTask) -> StructuredOutputMode:
        del task
        return StructuredOutputMode.TEXT_JSON

    def protocol(self, task: ModelTask) -> ModelProtocol:
        del task
        return ModelProtocol.CHAT_COMPLETIONS

    def capabilities(self, task: ModelTask) -> frozenset[ModelCapability]:
        del task
        return frozenset()


class _PersistingTTS:
    def __init__(
        self,
        generations: SpeechGenerationRepository,
        *,
        profile_id: str,
        reference_id: int,
    ) -> None:
        self.requests: list[SpeechSynthesisRequest] = []
        self._generations = generations
        self._profile_id = profile_id
        self._reference_id = reference_id

    async def synthesize(
        self,
        request: SpeechSynthesisRequest,
        *,
        cancellation: object = None,
    ) -> SynthesizedSpeech:
        del cancellation
        self.requests.append(request)
        generation = await self._generations.create(
            request_id=request.request_id,
            conversation_key_hash="a" * 64,
            trigger_event_id=request.trigger_event_id,
            profile_id=self._profile_id,
            reference_id=self._reference_id,
            engine_version="v2",
            target_language="zh",
            text_hash="b" * 64,
            normalized_text_hash="c" * 64,
            character_count=max(1, len(request.text)),
            cache_key=request.request_id,
            expires_at=None,
            canonical_conversation_id=request.canonical_conversation_id,
        )
        return SynthesizedSpeech(
            generation.id,
            self._profile_id,
            "neutral",
            "zh",
            "cache/c24b.wav",
            "wav",
            32_000,
            1,
            120,
            False,
        )

    async def health(self) -> SpeechProviderHealth:
        return SpeechProviderHealth(True, True, True, False, self._profile_id)

    async def close(self) -> None:
        return None


async def _flip_v2(database: Database) -> None:
    async with database.sessions() as session, session.begin():
        row = await session.get(IdentityRuntimeStateModel, 1)
        assert row is not None
        row.state = "v2"
        row.cutover_id = _CUTOVER
        row.source_fingerprint = "cutover-fingerprint"
        row.completed_at = _NOW


async def _add_conversation(
    session: Any,
    *,
    conversation_id: str,
    person_id: str,
    scope_key: str,
) -> None:
    alias_id = str(uuid4())
    session.add(
        CanonicalConversationModel(
            id=conversation_id,
            kind="private",
            person_id=person_id,
            space_id=None,
            primary_alias_id=alias_id,
            primary_marker=1,
            generation=1,
            starts_after_event_id=10_000,
            last_event_id=10_000,
            last_generation_change_event_id=10_000,
            covered_through_event_id=10_000,
            uncovered_event_count=0,
            uncovered_character_count=0,
            revision=1,
            created_at=_NOW,
            updated_at=_NOW,
        )
    )
    session.add(
        ConversationLegacyAliasModel(
            id=alias_id,
            conversation_id=conversation_id,
            scope_key=scope_key,
            is_primary=1,
            created_at=_NOW,
            updated_at=_NOW,
        )
    )


async def _seed_pair(database: Database) -> tuple[str, str, str, str, str]:
    conversation_a = str(uuid4())
    conversation_b = str(uuid4())
    async with database.sessions() as session, session.begin():
        person_a = await ensure_canonical_person_preconfig(session, "1001", now=_NOW)
        person_b = await ensure_canonical_person_preconfig(session, "1002", now=_NOW)
        presence_a = await ensure_canonical_presence_preconfig(session, "8000", now=_NOW)
        presence_b = await ensure_canonical_presence_preconfig(session, "8001", now=_NOW)
        await _add_conversation(
            session,
            conversation_id=conversation_a,
            person_id=person_a,
            scope_key="bot:8000:private:1001",
        )
        await _add_conversation(
            session,
            conversation_id=conversation_b,
            person_id=person_b,
            scope_key="bot:8001:private:1002",
        )
    return conversation_a, conversation_b, person_a, presence_a, presence_b


def _event(
    *,
    message_id: str,
    conversation_id: str,
    bot_user_id: str = "8000",
    ingress_presence_id: str | None = None,
    sender_user_id: str = "1001",
) -> Any:
    from qq_ai_bot.persistence.models import ChatEventModel

    return ChatEventModel(
        bot_user_id=bot_user_id,
        platform_message_id=message_id,
        scope_type="private",
        private_peer_user_id=sender_user_id,
        sender_user_id=sender_user_id,
        direction="inbound",
        event_kind="message",
        content="hi",
        visual_summary="",
        segments_json="[]",
        origin="user_message",
        occurred_at=_NOW,
        observed_at=_NOW,
        canonical_event_id=str(uuid4()),
        canonical_conversation_id=conversation_id,
        ingress_presence_id=ingress_presence_id,
        suppression_status="keeper",
    )


def _inbound(
    *,
    message_id: str = "c24b-msg",
    conversation_id: str | None = None,
    presence_id: str | None = None,
    bot_user_id: str = "8000",
    user_id: str = "1001",
) -> InboundMessage:
    return InboundMessage(
        message_id=message_id,
        event_type="message",
        scope_type=ScopeType.PRIVATE,
        sender=SenderIdentity(user_id=user_id, nickname="Tester"),
        text="hello",
        bot_user_id=bot_user_id,
        received_at=_NOW,
        conversation_id=conversation_id,
        presence_id=presence_id,
        legacy_conversation_key=f"bot:{bot_user_id}:private:{user_id}",
    )


def _executor(
    provider: object,
    *,
    invocations: ModelInvocationRepository | None = None,
) -> TaskModelExecutor:
    profile = ModelProfile(
        id="test",
        provider="fake",
        protocol=ModelProtocol.CHAT_COMPLETIONS,
        model="fake",
        timeout_seconds=5,
        max_retries=0,
        default_temperature=0,
        default_max_output_tokens=128,
        thinking_enabled=False,
        structured_output_mode=StructuredOutputMode.FUNCTION_TOOL,
        capabilities=frozenset(ModelCapability),
    )
    routes = {
        task: ModelRoute(task=task, profile_id="test", required_capabilities=frozenset())
        for task in ModelTask
    }
    return TaskModelExecutor(
        router=ModelRouter(ModelProfileCatalog(profiles={"test": profile}, routes=routes)),
        pool=ModelClientPool(injected_profiles={"test": cast(Any, provider)}),
        invocations=invocations,
    )


def _attach_executor(chat: ChatService, executor: TaskModelExecutor) -> None:
    chat._models = executor
    chat._agent_runner = AgentRunner(executor, chat._concurrency, web_router=chat._web_router)


async def _tool_runtime(
    chat: ChatService,
    inbound: InboundMessage,
) -> ToolRuntime:
    config = await chat._runtime_config.snapshot(user_id=inbound.sender.user_id)
    return ToolRuntime(
        inbound=inbound,
        gateway=None,
        allow_generic_onebot=False,
        conversation_key=inbound.legacy_conversation_key or "private:1001",
        trigger_message_id=inbound.message_id,
        actor_user_id=inbound.sender.user_id,
        runtime_config=config,
        origin=TurnOrigin.USER_MESSAGE,
    )


def _web_response() -> WebSearchResponse:
    return WebSearchResponse(
        query="now",
        sources=(
            WebSearchSource(
                source_id="s1",
                title="Example",
                url="https://example.com/article",
                domain="example.com",
                snippet="ok",
                relevant_content="",
            ),
        ),
        provider_request_id=None,
        latency_seconds=0.1,
    )


async def _seed_voice(database: Database) -> tuple[str, int]:
    from tests.unit.test_complete_v2_c24_conversation import _seed_voice as seed

    return await seed(database)


def _speech_runtime() -> SpeechRuntimeConfig:
    return SpeechRuntimeConfig(
        enabled=True,
        provider="genie",
        socket_path="/run/yuki-speech/genie.sock",
        root="/data/speech",
        genie_data_dir="/data/speech/genie_data",
        default_profile="c24b-voice",
        agent_effects_enabled=True,
        default_mode="optional",
        split_sentence=True,
        max_synthesis_characters=None,
        queue_max_pending=None,
        cache_retention_hours=None,
        private_enabled=True,
        group_enabled=True,
        automation_enabled=True,
        plugin_enabled=True,
        text_fallback_enabled=True,
    )


def _speech_service(
    database: Database,
    tmp_path: Path,
    provider: _PersistingTTS,
) -> SpeechService:
    generations = SpeechGenerationRepository(database)
    paths = SpeechPathPolicy(tmp_path / "speech")
    paths.ensure_layout()
    (paths.root / "cache" / "c24b.wav").parent.mkdir(parents=True, exist_ok=True)
    (paths.root / "cache" / "c24b.wav").write_bytes(b"RIFF")
    return SpeechService(
        provider=provider,
        generations=generations,
        cache=SpeechCache(repository=generations, paths=paths),
        paths=paths,
        profiles=VoiceProfileRepository(database),
    )


async def _recorded_models(database: Database) -> list[ModelInvocationModel]:
    async with database.sessions() as session:
        return list(
            await session.scalars(select(ModelInvocationModel).order_by(ModelInvocationModel.id))
        )


def test_speech_request_shape_remains_backward_compatible() -> None:
    names = {item.name for item in fields(SpeechSynthesisRequest)}
    assert "canonical_conversation_id" in names
    request = SpeechSynthesisRequest(
        request_id="r1",
        profile_id="yuki",
        style_hint="",
        text="hi",
        split_sentence=True,
        conversation_key="private:1001",
        trigger_event_id=None,
        turn_token=None,
    )
    assert request.canonical_conversation_id is None
    assert "canonical_conversation_id" not in {item.name for item in fields(ChatRequest)}


@pytest.mark.asyncio
async def test_chat_model_and_cadence_success_failure_and_v1_none(database: Database) -> None:
    repository = ModelInvocationRepository(database)
    provider = FakeLLMProvider(lambda _request: "ok")
    executor = _executor(provider, invocations=repository)
    harness = build_harness(database, make_settings(database.url), provider)
    chat = harness.processor._chat
    _attach_executor(chat, executor)
    chat._reply_effects = ReplyEffectRepository(database)

    inbound = _inbound()
    runtime = await _tool_runtime(chat, inbound)
    await chat._run_agent("private:1001", (ChatMessage(role="user", content="v1"),), runtime)
    await chat._record_reply_effects(
        conversation_key="private:1001",
        source_event_id="v1-src",
        user_id="1001",
        control=ReplyControlState(spec=default_reply_spec(hard_max_messages=2), text_sent=True),
        cancelled=False,
        inbound=inbound,
    )
    rows = await _recorded_models(database)
    assert len(rows) == 1
    assert rows[0].canonical_conversation_id is None
    async with database.sessions() as session:
        cadence = (await session.scalars(select(ReplyEffectEventModel))).one()
    assert cadence.canonical_conversation_id is None
    assert "canonical_conversation_id" not in asdict(provider.requests[0])

    await _flip_v2(database)
    conversation_id, _, person_id, presence_id, _ = await _seed_pair(database)
    stamped = _inbound(
        message_id="live-chat",
        conversation_id=conversation_id,
        presence_id=presence_id,
    )
    stamped_runtime = await _tool_runtime(chat, stamped)
    await chat._run_agent(
        "private:1001",
        (ChatMessage(role="user", content="ok"),),
        stamped_runtime,
    )
    failing = _executor(_FailingProvider(), invocations=repository)
    _attach_executor(chat, failing)
    with pytest.raises(RuntimeError, match="synthetic-provider-failure"):
        await chat._run_agent(
            "private:1001",
            (ChatMessage(role="user", content="fail"),),
            stamped_runtime,
        )
    await chat._record_reply_effects(
        conversation_key="private:1001",
        source_event_id="live-cadence",
        user_id="1001",
        control=ReplyControlState(spec=default_reply_spec(hard_max_messages=2), text_sent=True),
        cancelled=False,
        inbound=stamped,
    )
    rows = await _recorded_models(database)
    assert [row.success for row in rows[1:]] == [True, False]
    assert {row.canonical_conversation_id for row in rows[1:]} == {conversation_id}
    async with database.sessions() as session:
        cadence_rows = list(await session.scalars(select(ReplyEffectEventModel)))
    assert cadence_rows[-1].canonical_conversation_id == conversation_id

    _attach_executor(
        chat,
        _executor(FakeLLMProvider(lambda _request: "nope"), invocations=repository),
    )
    wrong = _inbound(message_id="wrong-kind", conversation_id=person_id, presence_id=presence_id)
    with pytest.raises(IdentityDualWriteError) as caught:
        await chat._run_agent(
            "private:1001",
            (ChatMessage(role="user", content="kind"),),
            await _tool_runtime(chat, wrong),
        )
    assert caught.value.category == CANONICAL_KIND_MISMATCH
    assert len(await _recorded_models(database)) == 3


@pytest.mark.asyncio
async def test_chat_web_mcp_and_presence_collision(database: Database) -> None:
    await _flip_v2(database)
    conversation_a, conversation_b, _, presence_a, presence_b = await _seed_pair(database)
    async with database.sessions() as session, session.begin():
        session.add(
            _event(
                message_id="shared-msg",
                conversation_id=conversation_a,
                bot_user_id="8000",
                ingress_presence_id=presence_a,
            )
        )
        session.add(
            _event(
                message_id="shared-msg",
                conversation_id=conversation_b,
                bot_user_id="8001",
                ingress_presence_id=presence_b,
            )
        )

    harness = build_harness(database, make_settings(database.url))
    chat = harness.processor._chat
    chat._tool_invocations = MCPRepository(database)
    chat._reply_effects = ReplyEffectRepository(database)
    inbound_a = _inbound(
        message_id="shared-msg",
        conversation_id=conversation_a,
        presence_id=presence_a,
        bot_user_id="8000",
    )
    await chat._save_native_web_response(
        inbound=inbound_a,
        conversation_key="private:1001",
        response=_web_response(),
        max_runs=4,
    )
    runtime = await _tool_runtime(chat, inbound_a)
    await chat._record_mcp_invocation(
        runtime=runtime,
        provider_id="mcp.test",
        tool_name="web_search",
        success=True,
        latency_seconds=0.01,
        result_size=1,
        artifact_created=False,
        error_category=None,
        result_excerpt="ok",
    )
    await chat._record_reply_effects(
        conversation_key="private:1001",
        source_event_id="shared-msg",
        user_id="1001",
        control=ReplyControlState(spec=default_reply_spec(hard_max_messages=2), text_sent=True),
        cancelled=False,
        inbound=inbound_a,
    )

    tools = AgentToolService(
        settings=make_settings(
            database.url,
            web_enabled=True,
            web_mode=WebMode.TAVILY,
            tavily_api_key="test-placeholder",
        ),
        ledger=EventLedgerRepository(database),
        memories=MemoryFactService(MemoryFactRepository(database)),
        actions=AgentActionRepository(database),
        web_provider=FakeWebSearchProvider(response=_web_response()),
        web_sources=WebSearchSourceRepository(database),
        runtime_config=chat._runtime_config,
    )
    await tools.execute(
        "web_search",
        json.dumps({"query": "now"}),
        runtime,
    )

    inbound_bare = _inbound(message_id="shared-msg", bot_user_id="")
    bare_runtime = await _tool_runtime(chat, inbound_bare)
    await chat._save_native_web_response(
        inbound=inbound_bare,
        conversation_key="private:1002",
        response=_web_response(),
        max_runs=4,
    )
    await chat._record_mcp_invocation(
        runtime=bare_runtime,
        provider_id="mcp.test",
        tool_name="web_search",
        success=True,
        latency_seconds=0.01,
        result_size=1,
        artifact_created=False,
        error_category=None,
        result_excerpt="bare",
    )
    await chat._record_reply_effects(
        conversation_key="private:1002",
        source_event_id="missing-cadence",
        user_id="1001",
        control=ReplyControlState(spec=default_reply_spec(hard_max_messages=2), text_sent=True),
        cancelled=False,
        inbound=inbound_bare,
    )

    cross = _inbound(
        message_id="shared-msg",
        conversation_id=conversation_a,
        presence_id=presence_b,
        bot_user_id="8001",
    )
    with pytest.raises(IdentityDualWriteError):
        await chat._save_native_web_response(
            inbound=cross,
            conversation_key="private:1003",
            response=_web_response(),
            max_runs=4,
        )

    async with database.sessions() as session:
        webs = list(await session.scalars(select(WebSearchRunModel).order_by(WebSearchRunModel.id)))
        tools_rows = list(
            await session.scalars(select(ToolInvocationModel).order_by(ToolInvocationModel.id))
        )
        cadence_rows = list(
            await session.scalars(select(ReplyEffectEventModel).order_by(ReplyEffectEventModel.id))
        )
    stamped_webs = [row.canonical_conversation_id for row in webs]
    stamped_tools = [row.canonical_conversation_id for row in tools_rows]
    stamped_cadence = [row.canonical_conversation_id for row in cadence_rows]
    assert conversation_a in stamped_webs
    assert conversation_a in stamped_tools
    assert conversation_a in stamped_cadence
    assert conversation_b not in stamped_webs
    assert conversation_b not in stamped_tools
    assert conversation_b not in stamped_cadence
    assert None in stamped_webs
    assert None in stamped_tools
    assert None in stamped_cadence


@pytest.mark.asyncio
async def test_voice_reply_effect_stamps_without_trigger_event(
    database: Database, tmp_path: Any
) -> None:
    await _flip_v2(database)
    conversation_id, _, person_id, _, _ = await _seed_pair(database)
    profile_id, reference_id = await _seed_voice(database)
    generations = SpeechGenerationRepository(database)
    tts = _PersistingTTS(generations, profile_id=profile_id, reference_id=reference_id)
    speech = _speech_service(database, tmp_path, tts)
    effects = VoiceReplyEffectService(speech)
    inbound = _inbound(conversation_id=conversation_id)
    token = SimpleNamespace(conversation_key="private:1001")
    snapshot = await RuntimeConfigService(
        settings=make_settings(database.url), database=database
    ).snapshot(user_id="1001")
    runtime = replace(snapshot, speech=_speech_runtime())

    prepared = await effects.prepare(
        inbound=inbound,
        response_text="你好",
        runtime=runtime,
        token=cast(Any, token),
        mode=VoiceMode.OPTIONAL,
        style_hint="",
    )
    assert prepared is not None
    assert tts.requests[0].trigger_event_id is None
    assert tts.requests[0].canonical_conversation_id == conversation_id
    async with database.sessions() as session:
        row = (await session.scalars(select(SpeechGenerationModel))).one()
    assert row.canonical_conversation_id == conversation_id
    assert row.trigger_event_id is None

    omitted = await effects.prepare(
        inbound=_inbound(),
        response_text="v1",
        runtime=runtime,
        token=cast(Any, token),
        mode=VoiceMode.OPTIONAL,
        style_hint="",
    )
    assert omitted is not None
    async with database.sessions() as session:
        rows = list(
            await session.scalars(select(SpeechGenerationModel).order_by(SpeechGenerationModel.id))
        )
    assert rows[-1].canonical_conversation_id is None

    tts_wrong = _PersistingTTS(generations, profile_id=profile_id, reference_id=reference_id)
    wrong_effects = VoiceReplyEffectService(_speech_service(database, tmp_path, tts_wrong))
    failed = await wrong_effects.prepare(
        inbound=_inbound(conversation_id=person_id),
        response_text="kind",
        runtime=runtime,
        token=cast(Any, token),
        mode=VoiceMode.OPTIONAL,
        style_hint="",
    )
    assert failed is None
    assert tts_wrong.requests[0].canonical_conversation_id == person_id


@pytest.mark.asyncio
async def test_plugin_facade_agent_and_speech_and_sessions(
    database: Database, tmp_path: Path
) -> None:
    await _flip_v2(database)
    conversation_id, _, person_id, presence_id, _ = await _seed_pair(database)
    profile_id, reference_id = await _seed_voice(database)
    settings = make_settings(database.url, speech_enabled=True)
    config = RuntimeConfigService(settings=settings, database=database)
    await config.initialize()
    capturing = _CapturingExecutor("plugin-ok")
    runner = AgentRunner(capturing, ConcurrencyManager(1))
    generations = SpeechGenerationRepository(database)
    tts = _PersistingTTS(generations, profile_id=profile_id, reference_id=reference_id)
    speech = _speech_service(database, tmp_path, tts)
    inbound = _inbound(conversation_id=conversation_id, message_id="plugin-msg")
    async with database.sessions() as session, session.begin():
        session.add(
            _event(
                message_id="plugin-msg",
                conversation_id=conversation_id,
                ingress_presence_id=presence_id,
            )
        )
        await session.flush()
        from qq_ai_bot.persistence.models import ChatEventModel

        event = await session.scalar(
            select(ChatEventModel).where(ChatEventModel.platform_message_id == "plugin-msg")
        )
        assert event is not None
        source_event_id = event.id
    snapshot = replace(await config.snapshot(user_id="1001"), speech=_speech_runtime())
    invocation = PluginInvocation(
        plugin_id="example.plugin",
        origin=TurnOrigin.USER_MESSAGE,
        actor_user_id=inbound.sender.user_id,
        bot_user_id=inbound.bot_user_id,
        inbound=inbound,
        runtime_config=snapshot,
        source_event_id=source_event_id,
    )
    assert invocation.conversation_id == conversation_id
    context = HostPluginContext(
        plugin_id="example.plugin",
        approved_permissions=(
            PluginPermission.LLM_GENERATE,
            PluginPermission.AGENT_RUN,
            PluginPermission.SPEECH_GENERATE,
        ),
        services=PluginFacadeServices(
            runtime_config=config,
            agent_runner=runner,
            speech=speech,
        ),
    )
    with context.bind(invocation):
        text = await context.llm.generate("hi")
        result = await context.agent.run("act")
        handle = await context.speech.synthesize("语音")
    assert text == "plugin-ok"
    assert result.data["text"] == "plugin-ok"
    assert capturing.conversation_ids == [conversation_id, conversation_id]
    assert all("canonical_conversation_id" not in asdict(item) for item in capturing.requests)
    assert tts.requests[-1].trigger_event_id == source_event_id
    assert tts.requests[-1].canonical_conversation_id == conversation_id
    assert handle.generation_id
    async with database.sessions() as session:
        speech_row = (await session.scalars(select(SpeechGenerationModel))).one()
    assert speech_row.canonical_conversation_id == conversation_id
    assert speech_row.trigger_event_id == source_event_id

    v1_invocation = PluginInvocation(
        plugin_id="example.plugin",
        origin=TurnOrigin.USER_MESSAGE,
        actor_user_id="1001",
        bot_user_id="8000",
        inbound=_inbound(),
        runtime_config=await config.snapshot(user_id="1001"),
    )
    with context.bind(v1_invocation):
        await context.llm.generate("v1")
    assert capturing.conversation_ids[-1] is None

    await PluginInstallationRepository(database).upsert_discovered(
        plugin_id="example.plugin",
        name="Session",
        version="0.1.0",
        plugin_api="2.0",
        yuki_requires=">=1.6.0,<2.0",
        manifest_hash="a" * 64,
        entrypoint="plugin:Plugin",
        requested_permissions=("agent.session",),
    )
    session_capturing = _CapturingExecutor("session-ok")
    sessions = PluginAgentSessionService(
        model_executor=session_capturing,
        concurrency=ConcurrencyManager(1),
        runtime_config=config,
        repository=PluginAgentSessionRepository(database),
    )
    facade = BoundAgentSessionFacade(
        service=sessions,
        plugin_id="example.plugin",
        actor_user_id="1001",
        current_group_id=None,
        approved_permissions=(PluginPermission.AGENT_SESSION,),
        conversation_id=conversation_id,
    )
    created = await facade.create(
        CreateAgentSessionRequest(
            name="isolated",
            instructions="stay isolated",
            persistence=SessionPersistence.DURABLE,
            context_profile=SessionContextProfile.NONE,
        )
    )
    await facade.run(RunAgentSessionRequest(session_id=created.session_id, user_input="hello"))
    assert session_capturing.conversation_ids == [conversation_id]
    assert getattr(created, "canonical_conversation_id", None) is None

    isolated = BoundAgentSessionFacade(
        service=PluginAgentSessionService(
            model_executor=_CapturingExecutor("none"),
            concurrency=ConcurrencyManager(1),
            runtime_config=config,
            repository=PluginAgentSessionRepository(database),
        ),
        plugin_id="example.plugin",
        actor_user_id="1001",
        current_group_id=None,
        approved_permissions=(PluginPermission.AGENT_SESSION,),
    )
    other = await isolated.create(
        CreateAgentSessionRequest(
            name="no-conversation",
            instructions="no conversation column",
            persistence=SessionPersistence.DURABLE,
            context_profile=SessionContextProfile.NONE,
        )
    )
    ran = await isolated.run(
        RunAgentSessionRequest(session_id=other.session_id, user_input="hello")
    )
    assert ran.text
    record = await PluginAgentSessionRepository(database).get(
        plugin_id="example.plugin",
        session_id=str(other.session_id),
    )
    assert record is not None
    assert (
        not hasattr(record, "canonical_conversation_id")
        or getattr(record, "canonical_conversation_id", None) is None
    )

    authority = PluginSessionAuthority(
        plugin_id="example.plugin",
        actor_user_id="1001",
        current_group_id=None,
        approved_permissions=frozenset({"agent.session"}),
        conversation_id=person_id,
    )
    with pytest.raises(IdentityDualWriteError) as caught:
        await PluginAgentSessionService(
            model_executor=_executor(
                FakeLLMProvider(lambda _request: "x"),
                invocations=ModelInvocationRepository(database),
            ),
            concurrency=ConcurrencyManager(1),
            runtime_config=config,
            repository=PluginAgentSessionRepository(database),
        ).run(
            authority,
            session_id=str(created.session_id),
            user_input="kind",
            allowed_capabilities=None,
            max_tool_calls=None,
            max_model_requests=None,
        )
    assert caught.value.category == CANONICAL_KIND_MISMATCH
