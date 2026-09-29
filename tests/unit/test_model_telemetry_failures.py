"""An observation failure must not replace the observed provider outcome."""

import asyncio
import sqlite3
from dataclasses import replace

import httpx
import pytest
from sqlalchemy import event
from sqlalchemy.exc import OperationalError
from tests.conftest import MemorySender, build_harness, make_settings
from tests.unit.test_commands_and_chat import inbound

from qq_ai_bot.domain.messages import (
    ChatMessage,
    ChatRequest,
    ChatResponse,
    ProviderContinuation,
    ToolCall,
    ToolFunction,
)
from qq_ai_bot.gateway.registry import RegistryClosed
from qq_ai_bot.identity.errors import CanonicalIdentityError
from qq_ai_bot.identity.routing import RouteSendError
from qq_ai_bot.llm.anthropic_messages import AnthropicMessagesProvider
from qq_ai_bot.llm.base import (
    LLMAuthenticationError,
    LLMEmptyResponseError,
    LLMInvalidRequestError,
    LLMInvalidResponseError,
    LLMTimeoutError,
    LLMUnavailableError,
)
from qq_ai_bot.llm.deepseek_responses import DeepSeekResponsesProvider
from qq_ai_bot.llm.fake import FakeLLMProvider
from qq_ai_bot.llm.gemini import GeminiProvider
from qq_ai_bot.llm.openai_compatible import OpenAICompatibleProvider
from qq_ai_bot.model_runtime.executor import TaskModelExecutor
from qq_ai_bot.model_runtime.models import (
    ModelCapability,
    ModelProfile,
    ModelProtocol,
    ModelRoute,
    ModelTask,
)
from qq_ai_bot.model_runtime.pool import ModelClientPool
from qq_ai_bot.model_runtime.profiles import ModelProfileCatalog
from qq_ai_bot.model_runtime.repository import ModelInvocationRepository
from qq_ai_bot.model_runtime.request_accounting import (
    ProviderAttemptCounter,
    current_provider_attempts,
)
from qq_ai_bot.model_runtime.routes import ModelRouter
from qq_ai_bot.runtime.activation_outcome import classify_failure, failure_status_text
from qq_ai_bot.runtime.work_repository import WorkConflict
from qq_ai_bot.services.main_agent_backend import UnsentFinalResponseError


def test_disconnected_presence_can_retry_without_reclassifying_route_denials():
    try:
        raise RouteSendError("original_presence_unavailable") from RegistryClosed("disconnected")
    except RouteSendError as disconnected:
        failure = classify_failure(disconnected)
    assert (failure.code, failure.stage, failure.retryable, failure.certainty) == (
        "gateway_disconnected",
        "gateway",
        True,
        "not_sent",
    )
    assert not classify_failure(RouteSendError("paused")).retryable
    try:
        raise RouteSendError("original_presence_unavailable") from RegistryClosed("ambiguous")
    except RouteSendError as ambiguous:
        assert not classify_failure(ambiguous).retryable


def test_work_conflict_receipt_keeps_safe_reason_without_exposing_arbitrary_text():
    changed = classify_failure(WorkConflict("work_journal_source_changed"))
    assert (changed.code, changed.stage, changed.retryable) == (
        "work_journal_source_changed",
        "context",
        True,
    )
    assert changed.diagnostics == {"category": "work_conflict"}
    stale = classify_failure(WorkConflict("work_effect_receipt_conflict"))
    assert stale.code == "work_effect_receipt_conflict" and not stale.retryable
    assert "状态" in failure_status_text(stale)
    unsafe = classify_failure(WorkConflict("private input: secret"))
    assert unsafe.code == "work_conflict_unspecified"
    assert "secret" not in repr(unsafe)


def busy():
    return OperationalError("INSERT", {}, sqlite3.OperationalError("database is locked"))


def executor(provider, telemetry, protocol=ModelProtocol.CHAT_COMPLETIONS):
    profile = ModelProfile(
        id="test",
        provider="fake",
        protocol=protocol,
        model="fake",
        base_url="http://test.invalid",
        api_key_env="TEST_KEY",
        timeout_seconds=2,
        max_retries=0,
        default_temperature=0.1,
        default_max_output_tokens=100,
        capabilities=frozenset(ModelCapability) - {ModelCapability.NATIVE_WEB_SEARCH},
    )
    return TaskModelExecutor(
        router=ModelRouter(
            ModelProfileCatalog(
                profiles={"test": profile},
                routes={task: ModelRoute(task=task, profile_id="test") for task in ModelTask},
            )
        ),
        pool=ModelClientPool(injected_profiles={"test": provider}),
        invocations=telemetry,
    )


async def test_telemetry_counts_dispatched_retry_and_unknown_usage_separately():
    requests = 0

    def transport(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        if requests == 1:
            return httpx.Response(503, json={"error": {"type": "overloaded"}})
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}
                ],
                "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
            },
        )

    class Telemetry:
        def __init__(self):
            self.records = []

        async def record(self, **values):
            self.records.append(values)

    async with httpx.AsyncClient(
        base_url="https://wire.invalid/v1/", transport=httpx.MockTransport(transport)
    ) as client:
        provider = OpenAICompatibleProvider(
            base_url="https://wire.invalid/v1/",
            api_key="test",
            timeout_seconds=2,
            max_retries=1,
            client=client,
            provider_name="openai",
        )
        telemetry = Telemetry()
        models = executor(provider, telemetry)
        try:
            response = await models.execute(
                ModelTask.CHAT_AGENT,
                ChatRequest(messages=(ChatMessage(role="user", content="hello"),)),
            )
        finally:
            await models.close()

    assert response.total_tokens == 8
    assert requests == 2
    assert len(telemetry.records) == 1  # One logical invocation.
    assert telemetry.records[0]["physical_request_count"] == 2
    assert telemetry.records[0]["unknown_usage_request_count"] == 1
    assert telemetry.records[0]["total_tokens"] == 8


@pytest.mark.parametrize(
    ("provider_type", "reported", "expected"),
    [
        (
            GeminiProvider,
            {
                "usageMetadata": {
                    "promptTokenCount": 9,
                    "candidatesTokenCount": 2,
                    "totalTokenCount": 11,
                    "cachedContentTokenCount": 3,
                }
            },
            {
                "prompt_tokens": 9,
                "completion_tokens": 2,
                "total_tokens": 11,
                "cached_prompt_tokens": 3,
            },
        ),
        (
            AnthropicMessagesProvider,
            {
                "usage": {
                    "input_tokens": 2,
                    "cache_read_input_tokens": 3,
                    "cache_creation_input_tokens": 4,
                    "output_tokens": 2,
                }
            },
            {
                "prompt_tokens": 9,
                "completion_tokens": 2,
                "total_tokens": 11,
                "cached_prompt_tokens": 3,
                "cache_creation_input_tokens": 4,
                "cache_creation_5m_input_tokens": None,
                "cache_creation_1h_input_tokens": None,
            },
        ),
        (
            OpenAICompatibleProvider,
            {
                "usage": {
                    "prompt_tokens": 9,
                    "completion_tokens": 2,
                    "prompt_tokens_details": {"cached_tokens": 3},
                }
            },
            {
                "prompt_tokens": 9,
                "completion_tokens": 2,
                "total_tokens": 11,
                "cached_prompt_tokens": 3,
                "reasoning_tokens": None,
            },
        ),
    ],
)
@pytest.mark.parametrize("has_usage", [True, False])
async def test_chat_http_error_preserves_only_reported_numeric_usage(
    provider_type, reported, expected, has_usage
):
    body = {"error": {"type": "bad_request"}, "private_extra": "do not record"}
    if has_usage:
        body.update(reported)
    async with httpx.AsyncClient(
        base_url="https://wire.invalid/v1/",
        transport=httpx.MockTransport(lambda _: httpx.Response(400, json=body)),
    ) as client:
        provider = provider_type(
            base_url="https://wire.invalid/v1/",
            api_key="test",
            timeout_seconds=2,
            max_retries=0,
            client=client,
        )
        attempts = ProviderAttemptCounter()
        token = current_provider_attempts.set(attempts)
        try:
            with pytest.raises(LLMInvalidRequestError) as caught:
                await provider.complete(
                    ChatRequest(messages=(ChatMessage(role="user", content="hello"),), model="test")
                )
        finally:
            current_provider_attempts.reset(token)
        assert caught.value.diagnostics["http_status"] == 400
        assert caught.value.diagnostics.get("usage") == (expected if has_usage else None)
        assert "private_extra" not in repr(caught.value.diagnostics)
        assert attempts.requests == 1
        assert attempts.unknown_usage_requests == (0 if has_usage else 1)


@pytest.mark.parametrize(
    ("status_code", "body", "error_type"),
    [
        (200, {"status": "failed", "output": []}, LLMUnavailableError),
        (200, {"status": "completed", "output": []}, LLMEmptyResponseError),
        (400, {"error": {"type": "bad_request"}}, LLMInvalidRequestError),
    ],
)
async def test_deepseek_responses_failed_body_keeps_known_usage_and_request_count(
    status_code, body, error_type
):
    class Telemetry:
        def __init__(self):
            self.records = []

        async def record(self, **values):
            self.records.append(values)

    response_body = {
        **body,
        "usage": {
            "input_tokens": 13,
            "output_tokens": 2,
            "input_tokens_details": {"cached_tokens": 4},
            "private_extra": "must not enter diagnostics",
        },
    }

    async with httpx.AsyncClient(
        base_url="https://api.deepseek.com/",
        transport=httpx.MockTransport(lambda _: httpx.Response(status_code, json=response_body)),
    ) as client:
        provider = DeepSeekResponsesProvider(
            base_url="https://api.deepseek.com",
            api_key="test",
            timeout_seconds=2,
            max_retries=0,
            client=client,
        )
        telemetry = Telemetry()
        models = executor(provider, telemetry, ModelProtocol.RESPONSES)
        try:
            with pytest.raises(error_type) as caught:
                await models.execute(
                    ModelTask.CHAT_AGENT,
                    ChatRequest(messages=(ChatMessage(role="user", content="hello"),)),
                )
        finally:
            await models.close()

    assert caught.value.diagnostics["usage"] == {
        "prompt_tokens": 13,
        "completion_tokens": 2,
        "total_tokens": 15,
        "cached_prompt_tokens": 4,
    }
    assert len(telemetry.records) == 1
    record = telemetry.records[0]
    assert record["success"] is False
    assert record["total_tokens"] == 15
    assert record["physical_request_count"] == 1
    assert record["unknown_usage_request_count"] == 0


@pytest.mark.parametrize("protocol", list(ModelProtocol))
@pytest.mark.parametrize("failed", [False, True])
async def test_telemetry_failure_preserves_response_or_original_error(failed, protocol, caplog):
    original = LLMTimeoutError("private provider detail")
    response = ChatResponse(
        content="private answer",
        latency_seconds=0.01,
        prompt_tokens=100,
        cached_prompt_tokens=90,
        tool_calls=(ToolCall("call_1", ToolFunction("workspace_read", '{"path":"secret"}')),),
        continuation=ProviderContinuation("fake", protocol.value, {"private": "history"}),
    )

    def respond(request):
        if failed:
            raise original
        return response

    class BrokenTelemetry:
        def __init__(self):
            self.records = []

        async def record(self, **values):
            # Commit may have succeeded before the error: never replay the INSERT.
            self.records.append(values)
            raise busy()

    provider = FakeLLMProvider(respond)
    telemetry = BrokenTelemetry()
    models = executor(provider, telemetry, protocol)
    request = ChatRequest(messages=(ChatMessage(role="user", content="hello"),), model="fake")
    try:
        if failed:
            with pytest.raises(LLMTimeoutError) as caught:
                await models.execute(ModelTask.CHAT_AGENT, request)
            assert caught.value is original
        else:
            actual = await models.execute(ModelTask.CHAT_AGENT, request)
            assert actual == replace(
                response, continuation=replace(response.continuation, profile_id="test")
            )
        assert len(provider.requests) == 1
        assert len(telemetry.records) == 1
        assert telemetry.records[0]["success"] is (not failed)
        assert "coverage_incomplete=true" in caplog.text
        assert "private" not in caplog.text
        assert "history" not in caplog.text
    finally:
        await models.close()


async def test_failed_provider_response_keeps_reported_usage_without_trusting_payload():
    failure = LLMInvalidResponseError(
        "blocked",
        diagnostics={
            "usage": {
                "prompt_tokens": 120,
                "completion_tokens": 4,
                "total_tokens": 124,
                "cached_prompt_tokens": 60,
                "cache_creation_input_tokens": 0,
                "cache_creation_5m_input_tokens": 0,
                "cache_creation_1h_input_tokens": 0,
                "untrusted": "secret",
            }
        },
    )

    class Telemetry:
        def __init__(self):
            self.records = []

        async def record(self, **values):
            self.records.append(values)

    def reject(_request):
        raise failure

    telemetry = Telemetry()
    models = executor(FakeLLMProvider(reject), telemetry)
    try:
        with pytest.raises(LLMInvalidResponseError) as caught:
            await models.execute(ModelTask.CHAT_AGENT, ChatRequest(messages=()))
        assert caught.value is failure
        assert telemetry.records[0]["success"] is False
        assert telemetry.records[0]["prompt_tokens"] == 120
        assert telemetry.records[0]["cached_prompt_tokens"] == 60
        assert telemetry.records[0]["cache_creation_input_tokens"] == 0
        assert telemetry.records[0]["cache_creation_5m_input_tokens"] == 0
        assert telemetry.records[0]["cache_creation_1h_input_tokens"] == 0
        assert telemetry.records[0]["total_tokens"] == 124
        assert "untrusted" not in telemetry.records[0]
    finally:
        await models.close()


async def test_missing_cache_read_keeps_model_invocation_total_unknown():
    failure = LLMInvalidResponseError(
        "blocked",
        diagnostics={
            "usage": {
                "prompt_tokens": None,
                "completion_tokens": 2,
                "total_tokens": None,
                "cached_prompt_tokens": None,
                "cache_creation_input_tokens": 5,
            }
        },
    )

    class Telemetry:
        def __init__(self):
            self.records = []

        async def record(self, **values):
            self.records.append(values)

    def reject(_request):
        raise failure

    telemetry = Telemetry()
    models = executor(FakeLLMProvider(reject), telemetry)
    try:
        with pytest.raises(LLMInvalidResponseError):
            await models.execute(ModelTask.CHAT_AGENT, ChatRequest(messages=()))
        recorded = telemetry.records[0]
        assert recorded["prompt_tokens"] is None
        assert recorded["total_tokens"] is None
        assert recorded["cached_prompt_tokens"] is None
        assert recorded["cache_creation_input_tokens"] == 5
    finally:
        await models.close()


@pytest.mark.parametrize("failed", [False, True])
async def test_telemetry_does_not_hide_identity_errors_or_cancellation(failed, caplog):
    original = LLMTimeoutError("provider detail")

    def respond(request):
        if failed:
            raise original
        return ChatResponse(content="answer", latency_seconds=0)

    class BrokenTelemetry:
        def __init__(self, error):
            self.error = error

        async def record(self, **values):
            raise self.error

    for telemetry_error in (
        CanonicalIdentityError("canonical_kind_mismatch"),
        TypeError("telemetry bug"),
        asyncio.CancelledError(),
    ):
        provider = FakeLLMProvider(respond)
        models = executor(provider, BrokenTelemetry(telemetry_error))
        expected = (
            original
            if failed and not isinstance(telemetry_error, asyncio.CancelledError)
            else telemetry_error
        )
        try:
            with pytest.raises(type(expected)) as caught:
                await models.execute(ModelTask.CHAT_AGENT, ChatRequest(messages=()))
            assert caught.value is expected
            assert len(provider.requests) == 1
            assert "telemetry bug" not in caplog.text
        finally:
            await models.close()


async def test_telemetry_correlation_reads_finish_before_write_lock(database):
    statements = []

    def observe(connection, cursor, statement, parameters, context, many):
        statements.append(statement)

    event.listen(database.engine.sync_engine, "before_cursor_execute", observe)
    try:
        await ModelInvocationRepository(database).record(
            task=ModelTask.CHAT_AGENT,
            profile_id="test",
            provider="fake",
            model="fake",
            success=True,
            prompt_tokens=100,
            completion_tokens=10,
            total_tokens=110,
            cached_prompt_tokens=90,
            latency_seconds=0,
            error_category=None,
            canonical_conversation_id="missing-conversation",
        )
    finally:
        event.remove(database.engine.sync_engine, "before_cursor_execute", observe)
    insert = next(i for i, sql in enumerate(statements) if sql.startswith("INSERT"))
    selects = [i for i, sql in enumerate(statements) if sql.startswith("SELECT")]
    assert selects and max(selects) < insert


async def test_claude_cache_write_tiers_persist_and_aggregate(database):
    telemetry = ModelInvocationRepository(database)
    saved = await telemetry.record(
        task=ModelTask.CHAT_AGENT,
        profile_id="claude",
        provider="anthropic",
        model="fixture",
        success=True,
        prompt_tokens=100,
        completion_tokens=5,
        total_tokens=105,
        cached_prompt_tokens=60,
        cache_creation_input_tokens=30,
        cache_creation_5m_input_tokens=20,
        cache_creation_1h_input_tokens=10,
        latency_seconds=0,
        error_category=None,
    )
    assert (
        saved.cache_creation_input_tokens,
        saved.cache_creation_5m_input_tokens,
        saved.cache_creation_1h_input_tokens,
    ) == (30, 20, 10)
    stats = (await telemetry.stats_by_profile())["claude"]
    assert (
        stats.cache_creation_input_tokens,
        stats.cache_creation_5m_input_tokens,
        stats.cache_creation_1h_input_tokens,
    ) == (30, 20, 10)


async def test_chat_internal_database_failure_is_not_reported_as_provider_outage(database):
    harness = build_harness(database, make_settings(database.url), FakeLLMProvider())

    async def fail(*args, **kwargs):
        raise busy()

    harness.processor._chat.handle_turn = fail
    sender = MemorySender()
    result = await harness.processor.handle(inbound("你好", message_id="busy-status"), sender)
    assert result.reason == "internal_failure"
    assert len(sender.messages) == 1
    assert "数据存储暂时繁忙" in sender.messages[0].text
    assert "AI 服务" not in sender.messages[0].text


def test_failure_status_uses_existing_runtime_classification():
    for error, expected in (
        (LLMAuthenticationError("secret"), "认证"),
        (LLMInvalidRequestError("secret"), "不兼容"),
        (LLMTimeoutError("secret"), "超时"),
        (KeyError("secret"), "内部错误"),
    ):
        status = failure_status_text(classify_failure(error))
        assert expected in status
        assert "secret" not in status


def test_unsent_final_is_agent_output_failure_not_provider_failure():
    failure = classify_failure(UnsentFinalResponseError("private model text"))
    assert (failure.code, failure.stage, failure.certainty) == (
        "unsent_final_response",
        "agent_output",
        "not_sent",
    )
    assert "没有发出" in failure_status_text(failure)
    assert "private model text" not in failure_status_text(failure)
