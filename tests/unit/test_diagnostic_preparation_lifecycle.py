"""Disposable preparation cannot occupy a model slot or outlive its reservation."""

import asyncio
import threading
import weakref
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import delete, event, select, update
from tests.support.social_identity_cases import social_env
from tests.unit.test_execution_trace import rows
from tests.unit.test_model_telemetry_failures import executor

from qq_ai_bot.conversation.rollup.models import RollupPolicyConfig
from qq_ai_bot.domain.conversations import ConversationScope
from qq_ai_bot.domain.messages import ChatMessage, ChatRequest
from qq_ai_bot.execution_trace import recorder as module
from qq_ai_bot.execution_trace.payload import PayloadCapacityError, freeze_payload
from qq_ai_bot.execution_trace.recorder import (
    TraceRecorder,
    current_trace,
    trace_span,
)
from qq_ai_bot.identity.canonical_repository import ensure_space
from qq_ai_bot.llm.fake import FakeLLMProvider
from qq_ai_bot.model_runtime.models import ModelTask
from qq_ai_bot.model_runtime.repository import ModelInvocationRepository
from qq_ai_bot.persistence.diagnostic_writer import DiagnosticWriter
from qq_ai_bot.persistence.models import ChatEventModel
from qq_ai_bot.persistence.people_repository import PeopleRepository
from qq_ai_bot.persistence.scoped_event_uow import ScopedEventLedgerUnitOfWork
from qq_ai_bot.social.db_models import SocialOperationModel


async def foreign_conversation(database, env):
    async with database.sessions() as session, session.begin():
        await ensure_space(session, "20002")
    await ScopedEventLedgerUnitOfWork(database, config=RollupPolicyConfig()).append(
        scope=ConversationScope.group(env.bot.self_id, "20002"),
        platform_message_id="synthetic-foreign",
        sender_user_id="10001",
        direction="inbound",
        content="synthetic",
    )
    async with database.sessions() as session:
        return await session.scalar(
            select(ChatEventModel.canonical_conversation_id).where(
                ChatEventModel.canonical_conversation_id != env.context.conversation_id
            )
        )


async def test_saturated_default_codec_pool_does_not_delay_next_model(database, monkeypatch):
    loop = asyncio.get_running_loop()
    pool = ThreadPoolExecutor(max_workers=1)
    release = threading.Event()
    held = pool.submit(release.wait)
    monkeypatch.setattr(loop, "_default_executor", pool)
    writer = DiagnosticWriter()
    await writer.start()
    models = executor(FakeLLMProvider(), None)
    models._max_concurrency = 1
    models.traces = TraceRecorder(database, writer=writer)
    try:
        for text in ("first", "second"):
            result = await asyncio.wait_for(
                models.execute(
                    ModelTask.CHAT_AGENT,
                    ChatRequest(messages=(ChatMessage("user", text),)),
                ),
                0.5,
            )
            assert result.content == f"FakeLLM: {text}"
            assert models._provider_active == 0
        assert not held.done()
        assert (await writer.health())["bytes"] > 0
    finally:
        release.set()
        await writer.close(drain_seconds=5)
        await models.close()
        pool.shutdown(wait=True)


async def test_multi_producer_reservations_include_raw_codec_peak_and_release_references(database):
    class Sensitive(str):
        __slots__ = ("__weakref__",)

    text = Sensitive("synthetic" * 1000)
    reference = weakref.ref(text)
    payload = {"text": text}
    prepared, reserved = freeze_payload(payload, 32 * 1024 * 1024)
    del prepared
    writer = DiagnosticWriter(max_bytes=reserved * 2)
    await writer.start()
    entered, release = asyncio.Event(), asyncio.Event()

    async def hold():
        entered.set()
        await release.wait()

    writer.submit("hold", 0, hold)
    await entered.wait()
    recorder = TraceRecorder(database, writer=writer)
    scope = module.TraceScope(
        recorder, await recorder.coverage(), "turn", "op", None, None, None, None, None
    )
    try:
        await asyncio.gather(*(recorder.append(scope, "custom", payload) for _ in range(8)))
        health = await writer.health()
        assert health["bytes"] == reserved * 2
        assert health["dropped"] == 6 and health["pending"] == 2
        del payload, text
        assert reference() is not None
        release.set()
        await writer.drain()
        assert (await writer.health())["bytes"] == 0
        assert reference() is None  # idle consumer has released its last snapshot
    finally:
        release.set()
        await writer.close()


@pytest.mark.parametrize("blocked", ["codec", "source", "delivery"])
async def test_n1_returns_and_releases_next_request_while_consumer_prepares(
    database, tmp_path, monkeypatch, blocked
):
    env = await social_env(database, tmp_path)
    async with database.sessions() as session:
        source = await session.scalar(select(ChatEventModel.id).order_by(ChatEventModel.id))
    entered, release = asyncio.Event(), asyncio.Event()
    thread_entered, thread_release = threading.Event(), threading.Event()
    codec = module._encode_payload_timed
    validate = module._require_trace_source

    def slow_codec(*args):
        thread_entered.set()
        assert thread_release.wait(5)
        return codec(*args)

    async def slow_source(*args):
        entered.set()
        await release.wait()
        return await validate(*args)

    if blocked == "codec":
        monkeypatch.setattr(module, "_encode_payload_timed", slow_codec)
    elif blocked == "source":
        monkeypatch.setattr(module, "_require_trace_source", slow_source)
    else:
        validate_delivery = module._require_trace_delivery

        async def slow_delivery(*args):
            entered.set()
            await release.wait()
            return await validate_delivery(*args)

        monkeypatch.setattr(module, "_require_trace_delivery", slow_delivery)
    writer = DiagnosticWriter()
    await writer.start()
    models = executor(FakeLLMProvider(), ModelInvocationRepository(database, writer=writer))
    models._max_concurrency = 1
    models.traces = TraceRecorder(database, writer=writer)
    try:
        async with trace_span(
            "turn",
            {},
            recorder=models.traces,
            conversation_id=env.context.conversation_id,
            source_event_id=source,
        ):
            if blocked == "delivery":
                # Even dependency preparation for a rejected delivery is disposable.
                await module.record_confirmed_delivery("missing-receipt", source)
            first = await asyncio.wait_for(
                models.execute(
                    ModelTask.CHAT_AGENT,
                    ChatRequest(messages=(ChatMessage("user", "one"),)),
                ),
                0.5,
            )
            assert first.content == "FakeLLM: one"
            if blocked == "codec":
                assert await asyncio.to_thread(thread_entered.wait, 2)
            else:
                await asyncio.wait_for(entered.wait(), 1)
            second = await asyncio.wait_for(
                models.execute(
                    ModelTask.CHAT_AGENT,
                    ChatRequest(messages=(ChatMessage("user", "two"),)),
                ),
                0.5,
            )
            assert second.content == "FakeLLM: two"
            assert models._provider_active == 0
    finally:
        release.set()
        thread_release.set()
        await writer.close(drain_seconds=5)
        await models.close()


@pytest.mark.parametrize("interrupt_close", [False, True])
async def test_shutdown_joins_real_codec_thread_before_freeing_bytes_or_writing(
    database, monkeypatch, interrupt_close
):
    entered, release = threading.Event(), threading.Event()
    original = module._encode_payload_timed

    def held_codec(*args):
        entered.set()
        assert release.wait(5)
        return original(*args)

    monkeypatch.setattr(module, "_encode_payload_timed", held_codec)
    writer = DiagnosticWriter()
    await writer.start()
    recorder = TraceRecorder(database, writer=writer)
    close = None
    try:
        async with trace_span("turn", {}, recorder=recorder):
            pass
        assert await asyncio.to_thread(entered.wait, 2)
        close = asyncio.create_task(writer.close(drain_seconds=0.01))
        await asyncio.sleep(0.05)
        assert not close.done()
        assert (await writer.health())["bytes"] > 0
        if interrupt_close:
            close.cancel()
            writer._task.cancel()
            await asyncio.sleep(0.01)
            assert not close.done()
            assert (await writer.health())["bytes"] > 0
        release.set()
        if interrupt_close:
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(close, 2)
        else:
            await asyncio.wait_for(close, 2)
        assert (await writer.health())["bytes"] == 0
        assert not await rows(database)
        await asyncio.sleep(0)
        assert not await rows(database)  # no late thread-completion INSERT
    finally:
        release.set()
        if close is not None:
            await asyncio.gather(close, return_exceptions=True)
        else:
            await writer.close()


@pytest.mark.parametrize("changed", ["deleted", "reowned"])
async def test_queued_source_cannot_be_accepted_as_a_different_owner(database, tmp_path, changed):
    env = await social_env(database, tmp_path)
    foreign = await foreign_conversation(database, env)
    async with database.sessions() as session:
        source = await session.scalar(select(ChatEventModel.id).order_by(ChatEventModel.id))
    entered, release = asyncio.Event(), asyncio.Event()
    writer = DiagnosticWriter()
    await writer.start()

    async def hold():
        entered.set()
        await release.wait()

    writer.submit("hold", 0, hold)
    await entered.wait()
    recorder = TraceRecorder(database, writer=writer)
    try:
        async with trace_span(
            "turn",
            {"old": ["frozen"]},
            recorder=recorder,
            conversation_id=env.context.conversation_id,
            source_event_id=source,
        ):
            coverage = current_trace.get().coverage
        async with database.sessions() as session, session.begin():
            if changed == "deleted":
                await session.execute(delete(ChatEventModel).where(ChatEventModel.id == source))
            else:
                await session.execute(
                    update(ChatEventModel)
                    .where(ChatEventModel.id == source)
                    .values(canonical_conversation_id=foreign)
                )
        release.set()
        await writer.drain()
        assert not await rows(database)
        assert coverage.failures == 2
        assert (await writer.health())["failures"] == 2
    finally:
        release.set()
        await writer.close()


async def test_source_race_between_reader_and_writer_is_conditionally_refused(
    database, tmp_path, monkeypatch
):
    env = await social_env(database, tmp_path)
    foreign = await foreign_conversation(database, env)
    async with database.sessions() as session:
        source = await session.scalar(select(ChatEventModel.id).order_by(ChatEventModel.id))
    recorder = TraceRecorder(database)
    codec = module._encode_payload_timed
    entered, release = threading.Event(), threading.Event()

    def barrier(*args):
        entered.set()
        assert release.wait(5)
        return codec(*args)

    monkeypatch.setattr(module, "_encode_payload_timed", barrier)
    coverage = await recorder.coverage()
    scope = module.TraceScope(
        recorder,
        coverage,
        "original",
        "original-op",
        None,
        env.context.conversation_id,
        None,
        None,
        source,
    )
    task = asyncio.create_task(recorder.append(scope, "custom", {}))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        async with database.sessions() as session, session.begin():
            await session.execute(
                update(ChatEventModel)
                .where(ChatEventModel.id == source)
                .values(canonical_conversation_id=foreign)
            )
        release.set()
        await task
        assert not await rows(database)
        assert coverage.failures == 1
    finally:
        release.set()
        await task


@pytest.mark.parametrize("shape", ["large_string", "media", "deep", "incompressible", "paths"])
def test_raw_and_temporary_resources_are_bounded_before_copy(shape):
    if shape == "deep":
        value = None
        for _ in range(100):
            value = [value]
    elif shape == "paths":
        value = {"parent" * 200: [{"image_url": "small"} for _ in range(300)]}
    else:
        value = (
            "x" * 100_000
            if shape != "incompressible"
            else "".join(chr(33 + i % 90) for i in range(100_000))
        )
        if shape == "media":
            value = {"type": "image", "source": {"data": value}}
    with pytest.raises(PayloadCapacityError):
        freeze_payload(value, 128 * 1024)


async def test_full_queue_does_not_copy_or_hash_payload(database, monkeypatch):
    writer = DiagnosticWriter(max_records=1)
    await writer.start()
    entered, release = asyncio.Event(), asyncio.Event()

    async def hold():
        entered.set()
        await release.wait()

    writer.submit("hold", 0, hold)
    await entered.wait()
    writer.submit("pending", 0, hold)
    recorder = TraceRecorder(database, writer=writer)

    def forbidden(*args):
        raise AssertionError("copy must not run")

    monkeypatch.setattr(module, "freeze_payload", forbidden)
    try:
        async with trace_span("turn", {"huge": "x" * 1_000_000}, recorder=recorder):
            pass
        assert recorder.record_failures == 2
        assert (await writer.health())["dropped"] == 2
    finally:
        release.set()
        await writer.close()


async def test_queued_job_freezes_privacy_scalar_even_if_live_coverage_changes(database):
    writer = DiagnosticWriter()
    await writer.start()
    entered, release = asyncio.Event(), asyncio.Event()

    async def hold():
        entered.set()
        await release.wait()

    writer.submit("hold", 0, hold)
    await entered.wait()
    recorder = TraceRecorder(database, writer=writer)
    try:
        async with trace_span("turn", {"private": ["original"]}, recorder=recorder):
            scope = current_trace.get()
            coverage = scope.coverage
        assert await PeopleRepository(database).delete_person("1001")
        coverage.privacy_generation = 1  # must not re-authorize old queued payload
        object.__setattr__(scope, "turn_id", "mutated-live-scope")
        release.set()
        await writer.drain()
        assert not await rows(database)
        assert coverage.failures == 2
        async with trace_span("turn", {"new": "allowed"}, recorder=recorder):
            pass
        await writer.drain()
        assert len(await rows(database)) == 2
    finally:
        release.set()
        await writer.close()


async def test_delivery_dependency_proof_reads_only_bounded_metadata(database, tmp_path):
    env = await social_env(database, tmp_path)
    sent = await env.service.execute("send_message", {"text": "synthetic outbound"}, env.context)
    async with database.sessions() as session:
        receipt = await session.scalar(
            select(SocialOperationModel.id).where(SocialOperationModel.event_id == sent["event_id"])
        )
    recorder = TraceRecorder(database)
    scope = module.TraceScope(
        recorder,
        await recorder.coverage(),
        "turn",
        "op",
        None,
        env.context.conversation_id,
        None,
        None,
        None,
    )
    selected = []

    def capture(_conn, _cursor, statement, *_args):
        if statement.lstrip().upper().startswith("SELECT"):
            selected.append(statement)

    event.listen(database.engine.sync_engine, "before_cursor_execute", capture)
    try:
        await recorder.append(scope, "social_delivery", {}, delivery=(receipt, sent["event_id"]))
    finally:
        event.remove(database.engine.sync_engine, "before_cursor_execute", capture)
    assert len(selected) == 2  # one source and one joined receipt/event metadata query
    assert not any(
        "chat_events.content" in sql
        or "chat_events.segments_json" in sql
        or "social_operation_receipts.platform_reference" in sql
        for sql in selected
    )
    assert scope.coverage.failures == 0
    assert (await rows(database))[0].delivered_event_id == sent["event_id"]


async def test_codec_failure_is_disposable_and_uncertain_commit_is_never_retried(
    database, monkeypatch
):
    writer = DiagnosticWriter()
    await writer.start()
    recorder = TraceRecorder(database, writer=writer)
    original = module._encode_payload_timed

    def fail(*args):
        raise TypeError("private compression detail")

    monkeypatch.setattr(module, "_encode_payload_timed", fail)
    try:
        async with trace_span("turn", {}, recorder=recorder):
            pass
        await writer.drain()
        assert recorder.record_failures == 2
        monkeypatch.setattr(module, "_encode_payload_timed", original)
        commits = 0
        commit = database.engine.sync_engine.dialect.do_commit

        def uncertain(connection):
            nonlocal commits
            commit(connection)
            commits += 1
            raise RuntimeError("result is unknown")

        monkeypatch.setattr(database.engine.sync_engine.dialect, "do_commit", uncertain)
        scope = module.TraceScope(
            recorder, await recorder.coverage(), "turn", "op", None, None, None, None, None
        )
        await recorder.append(scope, "custom", {})
        await writer.drain()
        assert commits == 1
        assert len(await rows(database)) == 1
    finally:
        await writer.close()


async def test_invocation_without_original_privacy_trace_is_dropped_without_sql(database):
    writer = DiagnosticWriter()
    await writer.start()
    repository = ModelInvocationRepository(database, writer=writer)
    queries = []

    def observe(_conn, _cursor, statement, *_args):
        queries.append(statement)

    event.listen(database.engine.sync_engine, "before_cursor_execute", observe)
    try:
        await repository.record(
            task=ModelTask.CHAT_AGENT,
            profile_id="fake",
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
        assert not queries
        assert (await writer.health())["dropped"] == 1
    finally:
        event.remove(database.engine.sync_engine, "before_cursor_execute", observe)
        await writer.close()
