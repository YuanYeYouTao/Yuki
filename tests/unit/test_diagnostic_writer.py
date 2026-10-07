"""Disposable diagnostics must neither delay responses nor inherit worker identity."""

import asyncio
from datetime import UTC, datetime

import pytest
from sqlalchemy import event, select
from tests.conftest import make_settings
from tests.support.model_profiles import write_fake_profiles
from tests.support.social_identity_cases import social_env
from tests.unit.test_execution_trace import decoded, rows
from tests.unit.test_model_telemetry_failures import executor

from qq_ai_bot.application.lifecycle import LifecycleRegistry
from qq_ai_bot.application.modules.model_runtime import ModelRuntimeModule
from qq_ai_bot.domain.messages import ChatMessage, ChatRequest
from qq_ai_bot.execution_trace.recorder import (
    TraceRecorder,
    current_trace,
    record_trace,
    trace_span,
)
from qq_ai_bot.llm.fake import FakeLLMProvider
from qq_ai_bot.model_runtime.db_models import ModelInvocationModel
from qq_ai_bot.model_runtime.models import ModelTask
from qq_ai_bot.model_runtime.repository import ModelInvocationRepository
from qq_ai_bot.persistence.diagnostic_writer import DiagnosticWriter
from qq_ai_bot.persistence.models import ChatEventModel
from qq_ai_bot.persistence.people_repository import PeopleRepository
from qq_ai_bot.runtime.observability import (
    RuntimeTurnCorrelation,
    bind_runtime_turn,
    current_runtime_turn_correlation,
)
from qq_ai_bot.runtime.origin import TurnOrigin
from qq_ai_bot.runtime.work_activation import current_work_control
from qq_ai_bot.runtime.work_control import WorkControl
from qq_ai_bot.runtime.work_repository import WorkLease, WorkRepository


async def test_provider_returns_and_releases_capacity_while_diagnostics_wait_for_writer(database):
    diagnostics = DiagnosticWriter()
    await diagnostics.start()
    models = executor(FakeLLMProvider(), ModelInvocationRepository(database, writer=diagnostics))
    models._max_concurrency = 1
    models.traces = TraceRecorder(database, writer=diagnostics)
    try:
        async with database.immediate_session():
            response = await asyncio.wait_for(
                models.execute(
                    ModelTask.CHAT_AGENT,
                    ChatRequest(messages=(ChatMessage("user", "hello"),)),
                ),
                timeout=1,
            )
            assert response.content == "FakeLLM: hello"
            assert models._provider_active == models._nonforeground_active == 0
            assert (await diagnostics.health())["bytes"] > 0
            # The first result has already left the executor, even though its
            # telemetry cannot commit. Another invocation can use its capacity.
            assert await asyncio.wait_for(
                models.execute(
                    ModelTask.CHAT_AGENT,
                    ChatRequest(messages=(ChatMessage("user", "second"),)),
                ),
                timeout=1,
            )
        await diagnostics.drain()
        assert (await models._invocations.stats()).invocations == 2
        assert any(row.kind == "model_end" for row in await rows(database))
    finally:
        await models.close()
        await diagnostics.close()


async def test_queued_records_freeze_original_ids_payload_and_timestamp(database, tmp_path):
    env = await social_env(database, tmp_path)
    async with database.sessions() as session:
        source = await session.scalar(select(ChatEventModel))
    diagnostics = DiagnosticWriter()
    release = asyncio.Event()
    entered = asyncio.Event()
    worker_context = []

    async def hold():
        worker_context.append((current_trace.get(), current_runtime_turn_correlation()))
        entered.set()
        await release.wait()

    # Start inside a different turn: the consumer itself must have no identity.
    with bind_runtime_turn(
        RuntimeTurnCorrelation("worker-must-not-inherit", TurnOrigin.SYSTEM_TASK)
    ):
        await diagnostics.start()
    diagnostics.submit("hold", 0, hold)
    await entered.wait()
    recorder = TraceRecorder(database, writer=diagnostics)
    telemetry = ModelInvocationRepository(database, writer=diagnostics)
    payload = {"text": "original"}
    correlation = RuntimeTurnCorrelation("original-turn", TurnOrigin.USER_MESSAGE)
    control = WorkControl(
        WorkRepository(database),
        WorkLease(env.context.conversation_id, 3, 0, 1, "original-activation"),
        "trusted-source",
        {"trigger_event_id": source.id},
        lambda: asyncio.sleep(0),
        current={"id": "original-work"},
    )
    try:
        token = current_work_control.set(control)
        with bind_runtime_turn(correlation):
            async with trace_span(
                "turn",
                {},
                recorder=recorder,
                conversation_id=env.context.conversation_id,
                execution_id="original-execution",
                source_event_id=source.id,
            ):
                await record_trace("provider_response", payload)
                await telemetry.record(
                    task=ModelTask.CHAT_AGENT,
                    profile_id="original-profile",
                    provider="fake",
                    model="fake",
                    success=True,
                    prompt_tokens=3,
                    completion_tokens=1,
                    total_tokens=4,
                    cached_prompt_tokens=0,
                    latency_seconds=0.01,
                    error_category=None,
                    canonical_conversation_id=env.context.conversation_id,
                )
        current_work_control.reset(token)
        payload["text"] = "mutated"
        control.current["id"] = "mutated-work"
        prepared_before = datetime.now(UTC).replace(tzinfo=None)
        with bind_runtime_turn(RuntimeTurnCorrelation("later-turn", TurnOrigin.SYSTEM_TASK)):
            release.set()
            await diagnostics.drain()
        evidence = await rows(database)
        assert worker_context == [(None, None)]
        assert {row.turn_id for row in evidence} == {"original-turn"}
        assert {row.execution_id for row in evidence} == {"original-execution"}
        assert {row.source_event_id for row in evidence} == {source.id}
        assert {row.work_id for row in evidence} == {"original-work"}
        assert {row.activation_id for row in evidence} == {"original-activation"}
        assert {row.generation for row in evidence} == {3}
        assert all(row.created_at <= prepared_before for row in evidence)
        assert decoded(next(row for row in evidence if row.kind == "provider_response"))[
            "data"
        ] == {"text": "original"}
        assert correlation.touched
        async with database.sessions() as session:
            invocation = await session.scalar(select(ModelInvocationModel))
        assert invocation.runtime_turn_id == "original-turn"
        assert invocation.canonical_conversation_id == env.context.conversation_id
    finally:
        release.set()
        await diagnostics.close()


async def test_privacy_erasure_fences_already_queued_prompt_and_invocation(database):
    diagnostics = DiagnosticWriter()
    await diagnostics.start()
    release = asyncio.Event()
    entered = asyncio.Event()

    async def hold():
        entered.set()
        await release.wait()

    diagnostics.submit("hold", 0, hold)
    await entered.wait()
    recorder = TraceRecorder(database, writer=diagnostics)
    telemetry = ModelInvocationRepository(database, writer=diagnostics)
    try:
        async with trace_span("turn", {"text": "erased"}, recorder=recorder):
            coverage = current_trace.get().coverage
            await telemetry.record(
                task=ModelTask.CHAT_AGENT,
                profile_id="old",
                provider="fake",
                model="fake",
                success=True,
                prompt_tokens=1,
                completion_tokens=1,
                total_tokens=2,
                cached_prompt_tokens=0,
                latency_seconds=0,
                error_category=None,
            )
        assert await PeopleRepository(database).delete_person("1001")
        release.set()
        await diagnostics.drain()
        assert not await rows(database)
        assert (await telemetry.stats()).invocations == 0
        assert coverage.failures == recorder.record_failures == 2
        async with trace_span("turn", {"text": "new"}, recorder=recorder):
            pass
        await diagnostics.drain()
        assert len(await rows(database)) == 2
    finally:
        release.set()
        await diagnostics.close()


async def test_queue_bounds_active_bytes_and_shutdown_reports_cancelled_diagnostics():
    diagnostics = DiagnosticWriter(max_records=1, max_bytes=10)
    await diagnostics.start()
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def hold():
        entered.set()
        try:
            await asyncio.Future()
        finally:
            cancelled.set()

    assert diagnostics.submit("active", 8, hold)
    await entered.wait()
    assert not diagnostics.submit("too_large", 3, hold)
    assert diagnostics.submit("pending", 2, hold)
    assert not diagnostics.submit("too_many", 0, hold)
    await diagnostics.close(drain_seconds=0.01)
    assert cancelled.is_set()
    health = await diagnostics.health()
    assert health["pending"] == health["bytes"] == 0
    assert health["dropped"] == 4
    assert not diagnostics.submit("after_close", 0, hold)
    with pytest.raises(ValueError):
        diagnostics.submit("negative", -1, hold)


async def test_async_sql_failure_updates_trace_coverage_without_retry(database, caplog):
    diagnostics = DiagnosticWriter()
    await diagnostics.start()
    recorder = TraceRecorder(database, writer=diagnostics)
    attempts = 0

    def fail(_connection, _cursor, statement, _parameters, *_args):
        nonlocal attempts
        if statement.startswith("INSERT INTO execution_trace_entries"):
            attempts += 1
            raise RuntimeError("private database error")

    event.listen(database.engine.sync_engine, "before_cursor_execute", fail)
    try:
        async with trace_span("turn", {}, recorder=recorder):
            coverage = current_trace.get().coverage
        await diagnostics.drain()
        assert attempts == 2
        assert coverage.failures == recorder.record_failures == 2
        assert (await diagnostics.health())["failures"] == 2
        assert "coverage_incomplete=true" in caplog.text
        assert "private database error" not in caplog.text
    finally:
        event.remove(database.engine.sync_engine, "before_cursor_execute", fail)
        await diagnostics.close()


async def test_application_starts_diagnostics_and_drains_them_before_database_close(
    database, tmp_path
):
    lifecycle = LifecycleRegistry()
    lifecycle.register("database", close=database.close)
    bundle = ModelRuntimeModule(
        make_settings(
            database.url, model_profiles_file=write_fake_profiles(tmp_path / "models.toml")
        ).model_runtime,
        database,
        lifecycle=lifecycle,
    ).build()
    assert lifecycle.names.index("database") < lifecycle.names.index("diagnostic_writer")
    assert lifecycle.names.index("diagnostic_writer") < lifecycle.names.index("model_runtime")
    await lifecycle.start()
    response = await bundle.executor.execute(
        ModelTask.CHAT_AGENT, ChatRequest(messages=(ChatMessage("user", "hello"),))
    )
    assert response.content
    await lifecycle.close()
    assert (await bundle.invocations.stats()).invocations == 1
