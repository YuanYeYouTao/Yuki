"""An observation failure must not replace the observed provider outcome."""

import asyncio
import sqlite3
from dataclasses import replace

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
from qq_ai_bot.llm.base import LLMAuthenticationError, LLMInvalidRequestError, LLMTimeoutError
from qq_ai_bot.llm.fake import FakeLLMProvider
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
from qq_ai_bot.model_runtime.routes import ModelRouter
from qq_ai_bot.runtime.activation_outcome import classify_failure, failure_status_text


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
        capabilities=frozenset(ModelCapability),
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
