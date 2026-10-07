"""Preparation metrics stay disposable, content-free and within the original root."""

import asyncio
from dataclasses import replace

import pytest
from sqlalchemy import select
from tests.conftest import MemorySender, build_harness, make_settings
from tests.support.fixed_contract_fixture import bind_main_contract
from tests.support.runtime_wire import install_wire
from tests.support.social_identity_cases import social_env
from tests.unit.test_commands_and_chat import inbound

from qq_ai_bot.domain.messages import ChatResponse, ToolCall, ToolFunction
from qq_ai_bot.execution_trace.db_models import ExecutionTraceEntryModel
from qq_ai_bot.execution_trace.payload import decode_payload
from qq_ai_bot.execution_trace.recorder import TraceRecorder, trace_span
from qq_ai_bot.identity.db_models import CanonicalSpaceModel
from qq_ai_bot.llm.fake import FakeLLMProvider
from qq_ai_bot.persistence.diagnostic_writer import DiagnosticWriter
from qq_ai_bot.services import chat_preparation_timings as timing


async def test_fixed_clock_main_stages_are_disjoint_and_details_are_subdivisions(monkeypatch):
    ticks = iter((10, 11, 13, 16, 17, 19, 20, 23, 26, 27, 30, 34))
    monkeypatch.setattr(timing, "perf_counter", lambda: next(ticks))
    recorded = []

    async def record(kind, payload):
        recorded.append((kind, payload))

    monkeypatch.setattr(timing, "record_trace", record)
    async with timing.collect_chat_preparation() as preparation:
        preparation.advance("runtime_snapshot")
        preparation.advance("memory_and_repair")
        preparation.advance("build_messages")
        with timing.preparation_detail("context_assembly"):
            pass
        with timing.preparation_detail("main_turn_composition"):
            pass
        preparation.advance("work_activation")
        preparation.advance("context_validation")
        preparation.advance("agent_setup")
        await timing.emit_chat_preparation()
        await timing.emit_chat_preparation()
    assert len(recorded) == 1
    kind, payload = recorded[0]
    assert kind == "chat_preparation"
    assert payload == {
        "status": "ready",
        "total_seconds": 24,
        "stage_seconds": {
            "work_preparation": 1,
            "runtime_snapshot": 2,
            "memory_and_repair": 3,
            "build_messages": 10,
            "work_activation": 1,
            "context_validation": 3,
            "agent_setup": 4,
        },
        "build_detail_seconds": {"context_assembly": 2, "main_turn_composition": 3},
    }
    assert sum(payload["stage_seconds"].values()) == payload["total_seconds"]


@pytest.mark.parametrize("failure", [RuntimeError, asyncio.CancelledError])
async def test_diagnostic_failure_does_not_change_exception_or_cancel_semantics(
    monkeypatch, failure
):
    async def unavailable(*_args):
        raise failure("diagnostic unavailable")

    monkeypatch.setattr(timing, "record_trace", unavailable)
    if failure is asyncio.CancelledError:
        with pytest.raises(asyncio.CancelledError):
            async with timing.collect_chat_preparation():
                await timing.emit_chat_preparation()
    else:
        async with timing.collect_chat_preparation():
            await timing.emit_chat_preparation()
        original = ValueError("original preparation failure")
        with pytest.raises(ValueError) as caught:
            async with timing.collect_chat_preparation():
                raise original
        assert caught.value is original
    # ContextVar lifetime ends even after cancellation/failure.
    await timing.emit_chat_preparation()


async def test_preparation_record_does_not_wait_for_a_writer(database):
    writer = DiagnosticWriter()
    await writer.start()
    recorder = TraceRecorder(database, writer=writer)

    async def prepare():
        async with trace_span("chat_processing", {}, recorder=recorder):
            async with timing.collect_chat_preparation():
                await timing.emit_chat_preparation()

    try:
        async with database.immediate_session():
            await asyncio.wait_for(prepare(), 0.5)
        await writer.drain()
        async with database.sessions() as session:
            rows = list(
                await session.scalars(
                    select(ExecutionTraceEntryModel).order_by(ExecutionTraceEntryModel.id)
                )
            )
        assert [row.kind for row in rows] == [
            "chat_processing_start",
            "chat_preparation",
            "chat_processing_end",
        ]
        assert len({row.operation_id for row in rows}) == 1
        assert len({row.turn_id for row in rows}) == 1
    finally:
        await writer.close()


@pytest.mark.parametrize("failure", [SystemExit, KeyboardInterrupt, asyncio.CancelledError])
async def test_termination_does_not_await_diagnostics_and_restores_context(monkeypatch, failure):
    records = []

    async def record(*args):
        records.append(args)

    monkeypatch.setattr(timing, "record_trace", record)
    original = failure()
    with pytest.raises(failure) as caught:
        async with timing.collect_chat_preparation():
            raise original
    assert caught.value is original
    await timing.emit_chat_preparation()
    assert records == []


@pytest.mark.parametrize("outcome", ["ready", "error", "cancelled"])
async def test_real_chat_preparation_uses_root_ids_and_preserves_business_flow(
    database, tmp_path, monkeypatch, outcome
):
    env = await social_env(database, tmp_path)
    async with database.sessions() as session, session.begin():
        space = await session.get(CanonicalSpaceModel, env.space)
        space.enabled = True

    def respond(request):
        if len(fake.requests) == 1:
            return ChatResponse(
                "",
                0,
                tool_calls=(
                    ToolCall("delivery", ToolFunction("send_message", '{"text":"hello"}')),
                ),
            )
        return ChatResponse("内部收尾", 0)

    fake = FakeLLMProvider(respond)
    harness = build_harness(database, make_settings(database.url, enabled_groups_csv="20001"), fake)
    bind_main_contract(harness, tmp_path)
    chat = harness.processor._chat
    chat._tools.social_service = env.service
    client, captured = install_wire(chat, fake, "chat_completions")
    writer = DiagnosticWriter()
    await writer.start()
    chat._models.traces = TraceRecorder(database, writer=writer)
    entered = asyncio.Event()

    async def blocked_assembly(**_kwargs):
        entered.set()
        if outcome == "error":
            raise ValueError("private body must never enter timing metadata")
        await asyncio.Event().wait()

    if outcome != "ready":
        monkeypatch.setattr(chat._context_assembler, "assemble", blocked_assembly)
    message = replace(
        inbound(
            "private source",
            message_id="timing-event",
            user_id="10001",
            group_id="20001",
            mentions_bot=True,
        ),
        bot_user_id="80001",
        conversation_id=env.context.conversation_id,
        person_id=env.person,
        space_id=env.space,
        presence_id=env.presence,
    )
    sender = MemorySender()
    try:
        task = asyncio.create_task(harness.processor.handle(message, sender))
        if outcome == "cancelled":
            await asyncio.wait_for(entered.wait(), 2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            result = await task
            assert result.reason == ("chat" if outcome == "ready" else "internal_failure")
        await writer.drain()
        async with database.sessions() as session:
            rows = list(
                await session.scalars(
                    select(ExecutionTraceEntryModel).order_by(ExecutionTraceEntryModel.id)
                )
            )
        root = next(row for row in rows if row.kind == "chat_processing_start")
        preparations = [row for row in rows if row.kind == "chat_preparation"]
        assert len(preparations) == (0 if outcome == "cancelled" else 1)
        if preparations:
            record = preparations[0]
            assert record.operation_id == root.operation_id
            assert record.turn_id == root.turn_id
            assert record.source_event_id == root.source_event_id
            assert record.source_event_id is not None
            payload = decode_payload(
                record.payload_gzip, size=record.payload_bytes, digest=record.payload_sha256
            )["data"]
            assert payload["status"] == outcome
            assert sum(payload["stage_seconds"].values()) == pytest.approx(payload["total_seconds"])
            assert all(value >= 0 for value in payload["stage_seconds"].values())
            assert set(payload) == {
                "status",
                "total_seconds",
                "stage_seconds",
                "build_detail_seconds",
            }
        if outcome == "ready":
            turn = next(row for row in rows if row.kind == "turn_start")
            first_model = next(row for row in rows if row.kind == "model_start")
            assert preparations[0].id < turn.id < first_model.id
            assert len(captured) == 2
            assert sum(action == "send_group_msg" for action, _ in env.bot.calls) == 1
        else:
            assert not captured
            # The original processor still sends its explicit failure notice;
            # cancellation sends nothing and never dispatches a business model.
            assert len(sender.messages) == int(outcome == "error")
            assert not any(action.startswith("send_") for action, _ in env.bot.calls)
            assert any(row.kind == "chat_processing_error" for row in rows)
    finally:
        await writer.close()
        await client.aclose()
