"""C24b-2a call-chain: model executor stamps trusted Conversation correlation."""

from __future__ import annotations

from dataclasses import asdict, fields, replace
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select

from qq_ai_bot.automation.authority import AuthorityContext
from qq_ai_bot.automation.handlers import AutomationCapabilityHandlers
from qq_ai_bot.automation.models import AutomationContext, TurnOrigin
from qq_ai_bot.automation.registry import CapabilityExecutionContext
from qq_ai_bot.conversation.canonical_db_models import (
    CanonicalConversationModel,
    ConversationLegacyAliasModel,
)
from qq_ai_bot.conversation.rollup.models import RollupCandidate, RollupKind, RollupPolicyConfig
from qq_ai_bot.conversation.rollup.service import ConversationRollupService
from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.domain.messages import ChatMessage, ChatRequest, ChatResponse
from qq_ai_bot.identity.c24_conversation import CANONICAL_KIND_MISMATCH
from qq_ai_bot.identity.db_models import IdentityRuntimeStateModel
from qq_ai_bot.identity.dual_write import ensure_canonical_person_preconfig
from qq_ai_bot.identity.errors import IdentityDualWriteError
from qq_ai_bot.llm.fake import FakeLLMProvider
from qq_ai_bot.model_runtime.db_models import ModelInvocationModel
from qq_ai_bot.model_runtime.executor import (
    LegacyTaskModelExecutor,
    TaskModelExecutor,
    request_shape_hash,
)
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
from qq_ai_bot.model_runtime.structured import StructuredTaskRunner
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.repository_records import EventRecord
from qq_ai_bot.services.agent_runner import AgentRunner, AgentRuntime
from qq_ai_bot.services.concurrency import ConcurrencyManager
from qq_ai_bot.time.models import TimeContext

_NOW = datetime(2026, 8, 25, tzinfo=UTC)
_CUTOVER = "550e8400-e29b-41d4-a716-4466554400c4"


class _CaptureOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    value: int


class _CapturingExecutor:
    def __init__(self, content: str = "hello") -> None:
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
    ) -> ChatResponse:
        del task, priority
        self.conversation_ids.append(canonical_conversation_id)
        self.requests.append(request)
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


class _FailingProvider:
    async def complete(self, request: ChatRequest) -> ChatResponse:
        del request
        raise RuntimeError("synthetic-provider-failure")

    async def close(self) -> None:
        return None


class _SpyAgentRunner:
    def __init__(self) -> None:
        self.runtime: AgentRuntime | None = None

    async def run(
        self,
        messages: tuple[ChatMessage, ...],
        runtime: AgentRuntime,
        tools: object,
    ) -> Any:
        del messages, tools
        self.runtime = runtime
        return SimpleNamespace(text="ok", tool_calls_used=0, model_requests=1)


async def _flip_v2(database: Database) -> None:
    async with database.sessions() as session, session.begin():
        row = await session.get(IdentityRuntimeStateModel, 1)
        assert row is not None
        row.state = "v2"
        row.cutover_id = _CUTOVER
        row.source_fingerprint = "cutover-fingerprint"
        row.completed_at = _NOW


async def _seed_conversation(database: Database) -> tuple[str, str]:
    conversation_id = str(uuid4())
    async with database.sessions() as session, session.begin():
        person_id = await ensure_canonical_person_preconfig(session, "1001", now=_NOW)
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
                scope_key="bot:8000:private:1001",
                is_primary=1,
                created_at=_NOW,
                updated_at=_NOW,
            )
        )
    return conversation_id, person_id


def _chat_request(content: str = "hello") -> ChatRequest:
    return ChatRequest(messages=(ChatMessage(role="user", content=content),))


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


def _agent_runtime(*, canonical_conversation_id: str | None = None) -> AgentRuntime:
    now = datetime.now(UTC)
    return AgentRuntime(
        origin=TurnOrigin.USER_MESSAGE,
        actor_user_id="1001",
        actor_is_superuser=False,
        delegated_authority=None,
        conversation_key="private:1001",
        current_group_id=None,
        bot_user_id="8000",
        gateway=None,
        runtime_config=cast(
            Any,
            SimpleNamespace(
                llm=SimpleNamespace(
                    model="fake",
                    temperature=0.1,
                    max_output_tokens=64,
                    thinking_enabled=False,
                )
            ),
        ),
        current_time=TimeContext(utc=now, local=now, timezone="Asia/Shanghai"),
        allowed_capabilities=frozenset(),
        max_tool_calls=1,
        max_model_requests=2,
        canonical_conversation_id=canonical_conversation_id,
    )


def _capability_context(
    *,
    canonical_conversation_id: str | None = None,
) -> CapabilityExecutionContext:
    now = datetime(2026, 8, 25, 8, tzinfo=UTC)
    return CapabilityExecutionContext(
        authority=AuthorityContext(
            origin=TurnOrigin.SCHEDULED_AUTOMATION,
            actor_user_id="1001",
            actor_is_superuser=False,
            bot_user_id="8000",
            delegated_authority=None,
            allowed_capabilities=frozenset(),
        ),
        automation_id=1,
        automation_run_id=2,
        step_id="generate",
        creator_user_id="1001",
        bot_user_id="8000",
        current_group_id=None,
        scheduled_for=now,
        actual_started_at=now,
        local_time=now,
        timezone="Asia/Shanghai",
        automation_context=AutomationContext(scene="none"),
        conversation_key="automation:1",
        canonical_conversation_id=canonical_conversation_id,
    )


def _rollup_candidate(*, conversation_id: str | None) -> RollupCandidate:
    return RollupCandidate(
        scope_id=1,
        generation=1,
        source_coverage=0,
        source_rollup_revision=0,
        previous_summary="",
        events=(
            EventRecord(
                id=11,
                bot_user_id="8000",
                platform_message_id="msg-11",
                scope_type=ScopeType.PRIVATE,
                sender_user_id="1001",
                direction="inbound",
                content="hello",
                visual_summary="",
                segments=(),
                occurred_at=_NOW,
                origin="user_message",
            ),
        ),
        event_count=1,
        projection_characters=5,
        fingerprint="c24b-executor",
        conversation_id=conversation_id,
    )


async def _recorded_rows(database: Database) -> list[ModelInvocationModel]:
    async with database.sessions() as session:
        return list(
            await session.scalars(select(ModelInvocationModel).order_by(ModelInvocationModel.id))
        )


def test_chat_request_shape_excludes_canonical_conversation() -> None:
    request = _chat_request("secret body")
    names = {item.name for item in fields(ChatRequest)}
    assert "canonical_conversation_id" not in names
    assert "canonical_conversation_id" not in asdict(request)
    digest = request_shape_hash(
        request,
        provider="fake",
        model="fake",
        profile_id="test",
        protocol=ModelProtocol.CHAT_COMPLETIONS.value,
    )
    assert digest == request_shape_hash(
        replace(request, messages=(ChatMessage(role="user", content="other body"),)),
        provider="fake",
        model="fake",
        profile_id="test",
        protocol=ModelProtocol.CHAT_COMPLETIONS.value,
    )


@pytest.mark.asyncio
async def test_legacy_executor_accepts_and_ignores_canonical_id() -> None:
    provider = FakeLLMProvider(lambda _request: "ok")
    executor = LegacyTaskModelExecutor(provider)
    conversation_id = str(uuid4())
    response = await executor.execute(
        ModelTask.CHAT_AGENT,
        _chat_request(),
        canonical_conversation_id=conversation_id,
    )
    assert response.content == "ok"
    assert "canonical_conversation_id" not in asdict(provider.requests[0])


@pytest.mark.asyncio
async def test_success_and_failure_records_stamp_trusted_id(database: Database) -> None:
    await _flip_v2(database)
    conversation_id, _person_id = await _seed_conversation(database)
    repository = ModelInvocationRepository(database)
    provider = FakeLLMProvider(lambda _request: "ok")
    executor = _executor(provider, invocations=repository)
    request = _chat_request("payload-a")

    await executor.execute(
        ModelTask.CHAT_AGENT,
        request,
        canonical_conversation_id=conversation_id,
    )
    failing = _executor(_FailingProvider(), invocations=repository)
    with pytest.raises(RuntimeError, match="synthetic-provider-failure"):
        await failing.execute(
            ModelTask.CHAT_AGENT,
            request,
            canonical_conversation_id=conversation_id,
        )
    await failing.close()
    await executor.close()

    rows = await _recorded_rows(database)
    assert [row.success for row in rows] == [True, False]
    assert {row.canonical_conversation_id for row in rows} == {conversation_id}
    assert provider.requests[0].request_shape_hash
    assert "canonical_conversation_id" not in asdict(provider.requests[0])


@pytest.mark.asyncio
async def test_missing_or_unknown_id_stays_null(database: Database) -> None:
    repository = ModelInvocationRepository(database)
    executor = _executor(FakeLLMProvider(lambda _request: "ok"), invocations=repository)
    await executor.execute(ModelTask.CHAT_AGENT, _chat_request("v1"))
    await _flip_v2(database)
    await _seed_conversation(database)
    await executor.execute(ModelTask.CHAT_AGENT, _chat_request("unknown"))
    await executor.execute(
        ModelTask.CHAT_AGENT,
        _chat_request("missing"),
        canonical_conversation_id=str(uuid4()),
    )
    await executor.close()
    rows = await _recorded_rows(database)
    assert len(rows) == 3
    assert all(row.canonical_conversation_id is None for row in rows)


@pytest.mark.asyncio
async def test_execute_does_not_change_request_shape_hash(database: Database) -> None:
    await _flip_v2(database)
    conversation_id, _person_id = await _seed_conversation(database)
    provider = FakeLLMProvider(lambda _request: "ok")
    executor = _executor(provider, invocations=ModelInvocationRepository(database))
    request = _chat_request("same-shape")
    await executor.execute(ModelTask.CHAT_AGENT, request)
    await executor.execute(
        ModelTask.CHAT_AGENT,
        request,
        canonical_conversation_id=conversation_id,
    )
    await executor.close()
    first, second = provider.requests
    assert first.request_shape_hash == second.request_shape_hash
    assert asdict(first) == asdict(second)
    expected = request_shape_hash(
        request,
        provider="fake",
        model="fake",
        profile_id="test",
        protocol=ModelProtocol.CHAT_COMPLETIONS.value,
    )
    assert first.request_shape_hash == expected


@pytest.mark.asyncio
async def test_wrong_kind_still_fails_closed(database: Database) -> None:
    await _flip_v2(database)
    _conversation_id, person_id = await _seed_conversation(database)
    executor = _executor(
        FakeLLMProvider(lambda _request: "ok"),
        invocations=ModelInvocationRepository(database),
    )
    with pytest.raises(IdentityDualWriteError) as caught:
        await executor.execute(
            ModelTask.CHAT_AGENT,
            _chat_request(),
            canonical_conversation_id=person_id,
        )
    await executor.close()
    assert caught.value.category == CANONICAL_KIND_MISMATCH
    assert await _recorded_rows(database) == []


@pytest.mark.asyncio
async def test_agent_runner_forwards_trusted_id_to_executor(database: Database) -> None:
    await _flip_v2(database)
    conversation_id, _person_id = await _seed_conversation(database)
    capturing = _CapturingExecutor("done")
    runner = AgentRunner(capturing, ConcurrencyManager(1))
    stamped = await runner.run(
        (_chat_request().messages[0],),
        _agent_runtime(canonical_conversation_id=conversation_id),
        tools=None,
    )
    omitted = await runner.run(
        (_chat_request().messages[0],),
        _agent_runtime(),
        tools=None,
    )
    assert stamped.text == "done"
    assert omitted.text == "done"
    assert capturing.conversation_ids == [conversation_id, None]
    assert all("canonical_conversation_id" not in asdict(item) for item in capturing.requests)

    repository = ModelInvocationRepository(database)
    executor = _executor(FakeLLMProvider(lambda _request: "chain"), invocations=repository)
    chained = AgentRunner(executor, ConcurrencyManager(1))
    await chained.run(
        (_chat_request().messages[0],),
        _agent_runtime(canonical_conversation_id=conversation_id),
        tools=None,
    )
    await chained.run((_chat_request().messages[0],), _agent_runtime(), tools=None)
    await executor.close()
    rows = await _recorded_rows(database)
    assert [row.canonical_conversation_id for row in rows] == [conversation_id, None]


@pytest.mark.asyncio
async def test_automation_generate_and_agent_propagate_context_id() -> None:
    conversation_id = str(uuid4())
    capturing = _CapturingExecutor("generated text")
    handlers = object.__new__(AutomationCapabilityHandlers)
    handlers._models = capturing
    handlers._concurrency = ConcurrencyManager(1)
    handlers._settings = SimpleNamespace(system_prompt="system")
    handlers._runtime_config = _SnapshotService()

    stamped = await handlers.generate(
        {"instruction": "say hi", "max_characters": 40},
        _capability_context(canonical_conversation_id=conversation_id),
    )
    omitted = await handlers.generate(
        {"instruction": "say hi", "max_characters": 40},
        _capability_context(),
    )
    assert stamped.data["text"] == "generated text"
    assert omitted.data["text"] == "generated text"
    assert capturing.conversation_ids == [conversation_id, None]

    spy = _SpyAgentRunner()
    handlers._registry = object()
    handlers._gateway_factory = lambda _context: object()
    handlers._agent_runner = spy
    handlers._time = SimpleNamespace(
        at=lambda *_args: TimeContext(utc=_NOW, local=_NOW, timezone="Asia/Shanghai")
    )
    await handlers.agent(
        {
            "instruction": "act",
            "max_tool_calls": 2,
            "max_model_requests": 3,
        },
        _capability_context(canonical_conversation_id=conversation_id),
    )
    assert spy.runtime is not None
    assert spy.runtime.canonical_conversation_id == conversation_id
    await handlers.agent(
        {
            "instruction": "act",
            "max_tool_calls": 2,
            "max_model_requests": 3,
        },
        _capability_context(),
    )
    assert spy.runtime is not None
    assert spy.runtime.canonical_conversation_id is None


@pytest.mark.asyncio
async def test_rollup_and_structured_wrappers_forward_trusted_id() -> None:
    conversation_id = str(uuid4())
    capturing = _CapturingExecutor("model summary")
    service = ConversationRollupService(
        models=capturing,
        config=RollupPolicyConfig(),
        timeout_seconds=1,
    )
    summary, kind = await service.summarize_candidate(
        _rollup_candidate(conversation_id=conversation_id)
    )
    omitted, _kind = await service.summarize_candidate(_rollup_candidate(conversation_id=None))
    assert kind is RollupKind.MODEL
    assert summary == "model summary"
    assert omitted == "model summary"
    assert capturing.conversation_ids == [conversation_id, None]

    structured_capture = _CapturingExecutor('{"value":4}')
    runner = StructuredTaskRunner(structured_capture)
    stamped = await runner.run(
        task=ModelTask.UTILITY_STRUCTURED,
        instruction="Return one object.",
        structured_input={"n": 1},
        output_model=_CaptureOutput,
        allow_text_json=True,
        canonical_conversation_id=conversation_id,
    )
    omitted_value = await runner.run(
        task=ModelTask.UTILITY_STRUCTURED,
        instruction="Return one object.",
        structured_input={"n": 1},
        output_model=_CaptureOutput,
        allow_text_json=True,
    )
    assert stamped.value == 4
    assert omitted_value.value == 4
    assert structured_capture.conversation_ids == [conversation_id, None]


class _SnapshotService:
    async def snapshot(self, **_kwargs: object) -> Any:
        return SimpleNamespace(
            llm=SimpleNamespace(
                model="fake",
                temperature=0.1,
                max_output_tokens=64,
                thinking_enabled=False,
            ),
            agent=SimpleNamespace(max_tool_calls=6, max_model_requests=8),
        )
