"""Logical exclusive phases and nested physical attempts use one monotonic clock."""

import asyncio
import time

import httpx
import pytest
from tests.unit.test_execution_trace import decoded, rows
from tests.unit.test_model_telemetry_failures import executor

from qq_ai_bot.domain.messages import (
    ChatMessage,
    ChatRequest,
    ChatResponse,
    NativeToolDefinition,
    NativeToolType,
)
from qq_ai_bot.execution_trace.phases import current_model_phases
from qq_ai_bot.execution_trace.recorder import TraceRecorder
from qq_ai_bot.llm.base import LLMError, LLMUnsupportedFeatureError
from qq_ai_bot.llm.fake import FakeLLMProvider
from qq_ai_bot.llm.openai_compatible import OpenAICompatibleProvider
from qq_ai_bot.llm.openai_responses import OpenAIResponsesProvider
from qq_ai_bot.model_runtime.dispatch_guard import model_dispatch_guard
from qq_ai_bot.model_runtime.models import ModelCapability, ModelProtocol, ModelTask
from qq_ai_bot.persistence.diagnostic_writer import DiagnosticWriter


def assert_partition(value):
    assert value["phase_version"] == 1
    assert sum(value["exclusive_seconds"].values()) == pytest.approx(
        value["logical_call_seconds"],
        abs=0.0001,
    )
    assert all(seconds >= 0 for seconds in value["exclusive_seconds"].values())
    assert "nested_seconds" in value


async def phase_records(database):
    return [decoded(row)["data"] for row in await rows(database) if row.kind == "model_phases"]


@pytest.mark.parametrize("retries", [0, 1, 2])
@pytest.mark.parametrize("failed", [False, True])
async def test_http_retries_errors_and_transport_are_separate_nested_observations(
    database, retries, failed
):
    calls = 0
    handler_seconds = 0.0

    async def handler(request):
        nonlocal calls, handler_seconds
        calls += 1
        started = time.perf_counter()
        await asyncio.sleep(0.002)
        handler_seconds += time.perf_counter() - started
        if failed or calls <= retries:
            return httpx.Response(503, json={"error": {"type": "overloaded"}})
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }
                ]
            },
        )

    writer = DiagnosticWriter()
    await writer.start()
    async with httpx.AsyncClient(
        base_url="https://offline.invalid/", transport=httpx.MockTransport(handler)
    ) as client:
        provider = OpenAICompatibleProvider(
            base_url="https://offline.invalid/",
            api_key="test",
            timeout_seconds=2,
            max_retries=retries,
            client=client,
        )
        models = executor(provider, None)
        models.traces = TraceRecorder(database, writer=writer)
        try:
            if failed:
                with pytest.raises(LLMError):
                    await models.execute(ModelTask.CHAT_AGENT, ChatRequest(messages=()))
            else:
                assert (
                    await models.execute(
                        ModelTask.CHAT_AGENT,
                        ChatRequest(messages=()),
                    )
                ).content == "ok"
            await writer.drain()
            (value,) = await phase_records(database)
            assert_partition(value)
            assert value["outcome"] == ("error" if failed else "success")
            assert calls == value["physical_attempt_count"] == retries + 1
            assert value["nested_seconds"]["transport"] >= handler_seconds
            assert ("retry_backoff" in value["nested_seconds"]) == bool(retries)
            assert models._provider_active == 0
        finally:
            await writer.close()
            await models.close()


async def test_n1_queue_guard_barrier_and_cancel_keep_slot_lifetime(database, monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0

    def respond(request):
        nonlocal calls
        calls += 1
        return ChatResponse(content="ok", latency_seconds=0)

    async def guard():
        entered.set()
        await release.wait()

    writer = DiagnosticWriter()
    await writer.start()
    models = executor(FakeLLMProvider(respond), None)
    models._max_concurrency = 1
    models.traces = TraceRecorder(database, writer=writer)
    first = second = None
    try:
        with model_dispatch_guard(guard):
            first = asyncio.create_task(
                models.execute(ModelTask.CHAT_AGENT, ChatRequest(messages=()))
            )
        await entered.wait()
        queued = asyncio.Event()
        wait_for = models._priority_condition.wait_for

        async def observe_wait(predicate):
            if models._provider_foreground_waiting:
                queued.set()
            return await wait_for(predicate)

        monkeypatch.setattr(models._priority_condition, "wait_for", observe_wait)
        second = asyncio.create_task(models.execute(ModelTask.CHAT_AGENT, ChatRequest(messages=())))
        await asyncio.wait_for(queued.wait(), 1)
        assert calls == 0 and models._provider_active == 1
        second.cancel()
        with pytest.raises(asyncio.CancelledError):
            await second
        release.set()
        assert (await first).content == "ok"
        await writer.drain()
        values = await phase_records(database)
        assert len(values) == 2
        for value in values:
            assert_partition(value)
        cancelled = next(value for value in values if value["outcome"] == "cancelled")
        assert cancelled["exclusive_seconds"]["slot_wait"] > 0
        assert cancelled["physical_attempt_count"] == 0
        completed = next(value for value in values if value["outcome"] == "success")
        assert completed["nested_seconds"]["dispatch_preparation"] > 0
        assert models._provider_active == 0 and calls == 1
    finally:
        release.set()
        if first is not None:
            await asyncio.gather(first, return_exceptions=True)
        if second is not None:
            await asyncio.gather(second, return_exceptions=True)
        await writer.close()
        await models.close()


async def test_local_capacity_failure_has_zero_attempts_and_partitioned_preparation(database):
    provider = FakeLLMProvider()
    models = executor(provider, None)
    catalog = models._router.catalog
    profile = catalog.profiles["test"].model_copy(update={"max_input_tokens": 1})
    models.apply_catalog(catalog.model_copy(update={"profiles": {"test": profile}}), models._pool)
    writer = DiagnosticWriter()
    await writer.start()
    models.traces = TraceRecorder(database, writer=writer)
    try:
        with pytest.raises(LLMUnsupportedFeatureError):
            await models.execute(
                ModelTask.CHAT_AGENT,
                ChatRequest(messages=(ChatMessage("user", "synthetic capacity"),)),
            )
        await writer.drain()
        (value,) = await phase_records(database)
        assert_partition(value)
        assert value["outcome"] == "error" and value["physical_attempt_count"] == 0
        assert set(value["exclusive_seconds"]) == {"preparation"}
        assert not provider.requests
    finally:
        await writer.close()
        await models.close()


async def test_failed_provider_telemetry_after_slot_release_is_response_preparation(database):
    entered, release = asyncio.Event(), asyncio.Event()
    original = LLMUnsupportedFeatureError("provider fixed category")
    released_slot_seconds = []

    def failed(request):
        raise original

    class Telemetry:
        async def record(self, **values):
            assert models._provider_active == 0
            phases = current_model_phases.get()
            assert phases.phase == "response_preparation"
            released_slot_seconds.append(phases.exclusive["slot_hold"])
            entered.set()
            await release.wait()

    writer = DiagnosticWriter()
    await writer.start()
    models = executor(FakeLLMProvider(failed), Telemetry())
    models.traces = TraceRecorder(database, writer=writer)
    task = asyncio.create_task(models.execute(ModelTask.CHAT_AGENT, ChatRequest(messages=())))
    try:
        await entered.wait()
        await asyncio.sleep(0.025)
        release.set()
        with pytest.raises(LLMUnsupportedFeatureError) as caught:
            await task
        assert caught.value is original
        await writer.drain()
        (value,) = await phase_records(database)
        assert_partition(value)
        assert value["exclusive_seconds"]["response_preparation"] > 0
        assert value["exclusive_seconds"]["slot_hold"] == released_slot_seconds[0]
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await writer.close()
        await models.close()


async def test_native_tool_uncertain_transport_never_retries_and_counts_one_attempt(database):
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("uncertain", request=request)

    writer = DiagnosticWriter()
    await writer.start()
    async with httpx.AsyncClient(
        base_url="https://offline.invalid/", transport=httpx.MockTransport(handler)
    ) as client:
        provider = OpenAIResponsesProvider(
            base_url="https://offline.invalid/",
            api_key="test",
            timeout_seconds=2,
            max_retries=2,
            client=client,
        )
        models = executor(provider, None, ModelProtocol.RESPONSES)
        catalog = models._router.catalog
        profile = catalog.profiles["test"].model_copy(
            update={
                "capabilities": frozenset(ModelCapability),
                "provider": "openai",
            }
        )
        models.apply_catalog(
            catalog.model_copy(update={"profiles": {"test": profile}}), models._pool
        )
        models.traces = TraceRecorder(database, writer=writer)
        try:
            with pytest.raises(LLMError):
                await models.execute(
                    ModelTask.CHAT_AGENT,
                    ChatRequest(
                        messages=(),
                        native_tools=(NativeToolDefinition(NativeToolType.WEB_SEARCH),),
                    ),
                )
            await writer.drain()
            (value,) = await phase_records(database)
            assert_partition(value)
            assert calls == value["physical_attempt_count"] == 1
            assert "retry_backoff" not in value["nested_seconds"]
        finally:
            await writer.close()
            await models.close()
