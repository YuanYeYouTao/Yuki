"""Real effect receipts survive secondary audit failure without replaying dispatch."""

import asyncio
import hashlib
import json
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import delete, func, select, update
from tests.conftest import build_harness, make_settings
from tests.support.social_identity_cases import social_env
from tests.unit.test_self_initiative_memory import record, seed

from qq_ai_bot.capabilities import (
    CapabilityTrustSource,
    InProcessToolProvider,
    ToolProviderRegistry,
)
from qq_ai_bot.capabilities.invocation import current_invocation
from qq_ai_bot.conversation.canonical_db_models import (
    CanonicalConversationModel,
    SpaceActiveRouteModel,
    SpaceBindingIngestRouteModel,
)
from qq_ai_bot.domain.messages import ChatMessage, ChatTool, ToolCall, ToolFunction
from qq_ai_bot.execution_trace.db_models import ExecutionTraceStateModel
from qq_ai_bot.identity.canonical_repository import ensure_person, ensure_space
from qq_ai_bot.identity.db_models import SpaceBindingModel
from qq_ai_bot.mcp.repository import MCPRepository
from qq_ai_bot.memory.partition import MemoryPartitionResolutionError
from qq_ai_bot.persistence.diagnostic_writer import DiagnosticWriter
from qq_ai_bot.persistence.models import ChatEventModel, MemoryToolReceiptModel, ToolInvocationModel
from qq_ai_bot.persistence.people_repository import PeopleRepository
from qq_ai_bot.runtime.observability import RuntimeTurnCorrelation, bind_runtime_turn
from qq_ai_bot.runtime.origin import TurnOrigin
from qq_ai_bot.runtime.work_control import WorkControl
from qq_ai_bot.runtime.work_repository import WorkRepository
from qq_ai_bot.runtime.work_schema_v1 import effects
from qq_ai_bot.runtime.work_session import WorkSession, defer_tool_audit
from qq_ai_bot.services.agent_tools import ToolRuntime
from qq_ai_bot.services.chat import ChatService
from qq_ai_bot.services.main_agent_backend import MainAgentBackend
from qq_ai_bot.services.turn_transcript import TurnTranscript
from qq_ai_bot.social.db_models import SocialOperationModel


async def active_work(database, tmp_path):
    env = await social_env(database, tmp_path)
    repo = WorkRepository(database)
    lease = await repo.acquire(env.context.conversation_id, 1)
    assert lease

    async def validate():
        assert await repo.valid(lease)

    control = WorkControl(repo, lease, "tool-audit", {}, validate)
    control.current = await repo.accept(lease, source_key="tool-audit", source={}, goal="send")
    work = WorkSession(control, "contract")
    control.session = work
    work.transcript = TurnTranscript((ChatMessage("system", "test"),))
    async with database.sessions() as session:
        source = await session.scalar(select(ChatEventModel))
    runtime = ToolRuntime(
        inbound=None,
        gateway=None,
        allow_generic_onebot=False,
        conversation_key="audit",
        trigger_event_id=source.id,
        conversation_id=env.context.conversation_id,
        presence_id=env.presence,
        bot_user_id=env.bot.self_id,
        execution_id=f"event:{source.id}",
    )
    return env, work, runtime


async def invoke_audit(recorder, runtime, call_key, result='{"ok":true}'):
    await ChatService._record_mcp_invocation(
        SimpleNamespace(_tool_invocations=recorder),
        runtime=runtime,
        provider_id="social",
        tool_name="send_message",
        success=True,
        latency_seconds=0.1,
        result_size=len(result),
        artifact_created=False,
        error_category=None,
        result_excerpt=result,
        tool_call_id=call_key,
    )


@pytest.mark.parametrize("queued", [False, True])
async def test_social_success_and_original_work_call_survive_telemetry_failure(
    database, tmp_path, monkeypatch, caplog, queued
):
    env, work, runtime = await active_work(database, tmp_path)
    call = ToolCall("original-provider-call", ToolFunction("send_message", "{}"))
    key = work.call_key(call.id)
    writer = DiagnosticWriter() if queued else None
    if writer is not None:
        await writer.start()
    recorder = MCPRepository(database, writer=writer)
    audit_calls = []

    async def fail_telemetry(*_args):
        # This failure occurs after BOTH durable effect and sourced evidence commit.
        async with database.sessions() as session:
            assert await session.scalar(select(effects.c.state)) == "accepted"
            assert await session.scalar(select(func.count(MemoryToolReceiptModel.id))) == 1
        audit_calls.append(key)
        raise RuntimeError("synthetic secondary failure")

    monkeypatch.setattr(recorder, "_insert_telemetry", fail_telemetry)
    dispatched = 0
    chat = build_harness(database, make_settings(database.url)).processor._chat
    chat._tool_invocations = recorder
    runtime = replace(
        runtime, runtime_config=await chat._runtime_config.snapshot(), space_id=env.space
    )

    async def dispatch(_name, _arguments, _runtime):
        nonlocal dispatched
        dispatched += 1
        context = current_invocation.get()
        assert context is not None
        social = await env.service.execute(
            "send_message",
            {"target": {"kind": "space", "target_id": env.space}, "text": "confirmed"},
            replace(
                env.context, call_id=context.call_id, trigger_event_id=runtime.trigger_event_id
            ),
        )
        assert social["status"] == "succeeded"
        return {"ok": True, "data": social}

    registry = ToolProviderRegistry()
    registry.register(
        InProcessToolProvider(
            provider_id="core",
            source=CapabilityTrustSource.CORE,
            definitions=lambda _: (ChatTool("send_message", "send", {"type": "object"}),),
            execute=dispatch,
        )
    )
    backend = MainAgentBackend(chat, runtime)
    backend._catalog = registry.catalog(runtime)
    backend._callable_tool_names = {"send_message"}
    agent = SimpleNamespace(work_control=work.control)
    backend.begin_batch((call,), agent)

    async def invoke():
        result = await backend.execute(call.function.name, call.function.arguments, agent)
        assert audit_calls == []
        return result

    result = await work.execute(call, invoke, allow_pending=True)
    if writer is not None:
        await writer.close()
        assert writer.failures == 1
    assert "data" in json.loads(result), result
    assert json.loads(result)["data"]["status"] == "succeeded"
    assert await work.execute(call, invoke, allow_pending=True) == result
    assert dispatched == 1 and audit_calls == [key]
    async with database.sessions() as session:
        effect = (await session.execute(select(effects))).mappings().one()
        assert effect["effect_key"] == key and effect["state"] == "accepted"
        assert json.loads(effect["receipt_json"])["result"] == result
        social = await session.scalar(select(SocialOperationModel))
        assert social.status == "succeeded" and social.tool_call_id == call.id
        source = await session.scalar(select(MemoryToolReceiptModel))
        assert source.trigger_event_id == runtime.trigger_event_id
        assert source.canonical_space_id == env.space
        assert source.canonical_person_id is None and source.tool_call_id == key
    assert "coverage_incomplete=true" in caplog.text
    assert "synthetic secondary failure" not in caplog.text
    # Repair re-runs only the audit using the accepted result. It is idempotent
    # and has no external dispatch or independent permanent retry queue.
    await recorder.record_invocation(
        conversation_key=runtime.conversation_key,
        provider_id="core",
        tool_name="send_message",
        success=True,
        latency_seconds=0,
        result_size=len(result),
        artifact_created=False,
        error_category=None,
        trigger_event_id=runtime.trigger_event_id,
        bot_user_id=runtime.effective_bot_user_id,
        canonical_conversation_id=runtime.conversation_id,
        tool_call_id=key,
        execution_id=runtime.effective_execution_id,
        result_excerpt=result,
    )
    async with database.sessions() as session:
        assert await session.scalar(select(func.count(MemoryToolReceiptModel.id))) == 1


@pytest.mark.parametrize("mode", ["cancel_audit", "uncertain_accepted_commit"])
async def test_post_effect_cancel_or_uncertain_commit_never_runs_unconfirmed_audit(
    database, tmp_path, monkeypatch, mode
):
    _env, work, runtime = await active_work(database, tmp_path)
    call = ToolCall("original", ToolFunction("send_message", "{}"))
    audit = AsyncMock(side_effect=asyncio.CancelledError() if mode == "cancel_audit" else None)
    recorder = SimpleNamespace(record_invocation=audit)
    if mode == "uncertain_accepted_commit":
        original = work.control.repository.record_effect

        async def ambiguous(key, state, receipt):
            await original(key, state, receipt)
            if state == "accepted":
                raise RuntimeError("commit result unknown")

        monkeypatch.setattr(work.control.repository, "record_effect", ambiguous)

    async def invoke():
        await invoke_audit(recorder, runtime, work.call_key(call.id))
        return '{"ok":true}'

    with pytest.raises(asyncio.CancelledError if mode == "cancel_audit" else RuntimeError):
        await work.execute(call, invoke, allow_pending=True)
    async with database.sessions() as session:
        assert await session.scalar(select(effects.c.state)) == "accepted"
    assert audit.await_count == (1 if mode == "cancel_audit" else 0)
    assert not defer_tool_audit(work.call_key(call.id), AsyncMock())


async def test_independent_sdk_audit_identity_error_still_propagates(database, tmp_path):
    _env, _work, runtime = await active_work(database, tmp_path)
    recorder = SimpleNamespace(record_invocation=AsyncMock(side_effect=ValueError("source")))
    with pytest.raises(ValueError, match="source"):
        await invoke_audit(recorder, runtime, "sdk-call")
    recorder.record_invocation.assert_awaited_once()


@pytest.mark.parametrize("metadata_only", [False, True])
async def test_deferred_audit_cannot_refill_person_erased_before_accepted(
    database, tmp_path, monkeypatch, metadata_only
):
    _env, work, runtime = await active_work(database, tmp_path)
    async with database.sessions() as session, session.begin():
        await ensure_person(session, "10002")
    source_id = runtime.trigger_event_id
    if metadata_only:
        runtime = replace(runtime, trigger_event_id=None)
    writer = DiagnosticWriter()
    await writer.start()
    recorder = MCPRepository(database, writer=writer)
    call = ToolCall("original-erased-call", ToolFunction("send_message", "{}"))
    original = work.control.repository.record_effect
    dispatched = 0

    async def erased_before_accepted(key, state, receipt):
        if state == "accepted":
            # The audit is registered; privacy erasure in another Conversation
            # cannot authorize this old result, nor erase this Work's receipt.
            assert await PeopleRepository(database).delete_person("10002")
        await original(key, state, receipt)

    monkeypatch.setattr(work.control.repository, "record_effect", erased_before_accepted)

    async def invoke():
        nonlocal dispatched
        dispatched += 1
        await invoke_audit(recorder, runtime, work.call_key(call.id), "confirmed")
        return "confirmed"

    result = await work.execute(call, invoke, allow_pending=True)
    await writer.close()
    assert result == "confirmed" and dispatched == 1
    assert await work.journal.effect_result(work.call_key(call.id)) == result
    async with database.sessions() as session:
        assert await session.scalar(select(effects.c.state)) == "accepted"
        assert await session.get(ChatEventModel, source_id) is not None
        assert await session.scalar(select(ExecutionTraceStateModel.privacy_generation)) == 1
        assert await session.scalar(select(func.count(MemoryToolReceiptModel.id))) == 0
        assert await session.scalar(select(func.count(ToolInvocationModel.id))) == 0


@pytest.mark.parametrize("after_prepare", [False, True])
async def test_group_binding_move_cannot_reassign_original_event_receipt(
    database, tmp_path, monkeypatch, after_prepare
):
    env, work, runtime = await active_work(database, tmp_path)
    recorder = MCPRepository(database)
    call = ToolCall("original-owner-call", ToolFunction("send_message", "{}"))
    sessions = database.sessions
    destination = None

    async def move_binding():
        nonlocal destination
        async with sessions() as session, session.begin():
            destination = await ensure_space(session, "20002")
            binding = await session.scalar(
                select(SpaceBindingModel).where(SpaceBindingModel.external_space_id == "20001")
            )
            assert binding is not None and binding.space_id == env.space
            await session.execute(
                delete(SpaceActiveRouteModel).where(SpaceActiveRouteModel.space_id == env.space)
            )
            await session.execute(
                delete(SpaceBindingIngestRouteModel).where(
                    SpaceBindingIngestRouteModel.space_binding_id == binding.id
                )
            )
            binding.space_id = destination

    if after_prepare:
        record_invocation = recorder.record_invocation

        async def record_after_move(**kwargs):
            first = True

            @asynccontextmanager
            async def moved_after_prepare():
                nonlocal first
                preparation = first
                first = False
                async with sessions() as session:
                    yield session
                if preparation:
                    await move_binding()

            monkeypatch.setattr(database, "sessions", moved_after_prepare)
            try:
                await record_invocation(**kwargs)
            finally:
                monkeypatch.setattr(database, "sessions", sessions)

        monkeypatch.setattr(recorder, "record_invocation", record_after_move)

    async def invoke():
        await invoke_audit(recorder, runtime, work.call_key(call.id))
        if not after_prepare:
            await move_binding()
        return "confirmed original result"

    result = await work.execute(call, invoke, allow_pending=True)
    assert destination is not None and destination != env.space
    assert await work.journal.effect_result(work.call_key(call.id)) == result
    async with sessions() as session:
        assert await session.scalar(select(effects.c.state)) == "accepted"
        conversation = await session.get(CanonicalConversationModel, runtime.conversation_id)
        assert conversation.space_id == env.space
        assert (
            await session.scalar(
                select(SpaceBindingModel.space_id).where(
                    SpaceBindingModel.external_space_id == "20001"
                )
            )
            == destination
        )
        assert await session.scalar(select(func.count(MemoryToolReceiptModel.id))) == 0
        assert await session.scalar(select(func.count(ToolInvocationModel.id))) == 0


async def test_tool_telemetry_queue_freezes_origin_and_rejects_privacy_erasure(database, tmp_path):
    env = await social_env(database, tmp_path)
    writer = DiagnosticWriter()
    recorder = MCPRepository(database, writer=writer)
    await writer.start()
    release = asyncio.Event()
    assert writer.submit("blocked", 0, release.wait)
    with bind_runtime_turn(RuntimeTurnCorrelation("original-runtime", TurnOrigin.USER_MESSAGE)):
        # Pure metadata has no event/Memory source and no synchronous INSERT.
        async with database.immediate_session():
            await asyncio.wait_for(
                recorder.record_invocation(
                    conversation_key="metadata",
                    provider_id="core",
                    tool_name="read",
                    success=True,
                    latency_seconds=0,
                    result_size=1,
                    artifact_created=False,
                    error_category=None,
                    canonical_conversation_id=env.context.conversation_id,
                ),
                timeout=1,
            )
    release.set()
    await writer.close()
    async with database.sessions() as session:
        row = await session.scalar(select(ToolInvocationModel))
        assert row.runtime_turn_id == "original-runtime"
        assert row.canonical_conversation_id == env.context.conversation_id

    writer = DiagnosticWriter()
    recorder.writer = writer
    await writer.start()
    release = asyncio.Event()
    assert writer.submit("blocked", 0, release.wait)
    await recorder.record_invocation(
        conversation_key="metadata",
        provider_id="core",
        tool_name="erased",
        success=True,
        latency_seconds=0,
        result_size=1,
        artifact_created=False,
        error_category=None,
        canonical_conversation_id=env.context.conversation_id,
    )
    async with database.sessions() as session, session.begin():
        session.add(ExecutionTraceStateModel(id=1, privacy_generation=1))
    release.set()
    await writer.close()
    async with database.sessions() as session:
        assert await session.scalar(select(func.count(ToolInvocationModel.id))) == 1


@pytest.mark.parametrize("change", ["privacy", "owner_binding", "generation"])
async def test_event_receipt_source_rechecked_atomically_after_read_prepare(
    database, tmp_path, monkeypatch, change
):
    env = await social_env(database, tmp_path)
    async with database.sessions() as session:
        source = await session.scalar(select(ChatEventModel))
    original = database.sessions
    first = True

    @asynccontextmanager
    async def changed_after_prepare():
        nonlocal first
        is_prepare = first
        first = False
        async with original() as session:
            yield session
        if is_prepare:
            async with original() as session, session.begin():
                if change == "privacy":
                    session.add(ExecutionTraceStateModel(id=1, privacy_generation=1))
                elif change == "owner_binding":
                    await session.execute(update(SpaceBindingModel).values(status="disabled"))
                else:
                    await session.execute(update(CanonicalConversationModel).values(generation=2))

    monkeypatch.setattr(database, "sessions", changed_after_prepare)
    with pytest.raises(MemoryPartitionResolutionError, match="tool_receipt_source_changed"):
        await MCPRepository(database).record_invocation(
            conversation_key="source",
            provider_id="core",
            tool_name="terminal_exec",
            success=True,
            latency_seconds=0,
            result_size=1,
            artifact_created=False,
            error_category=None,
            trigger_event_id=source.id,
            canonical_conversation_id=env.context.conversation_id,
            tool_call_id="original-call",
            execution_id="execution-1",
            bot_user_id=source.bot_user_id,
            result_excerpt="verified source",
        )
    async with original() as session:
        assert await session.scalar(select(func.count(MemoryToolReceiptModel.id))) == 0
        assert await session.scalar(select(func.count(ToolInvocationModel.id))) == 0


async def test_self_receipt_keeps_existing_call_key_and_refuses_erased_generation(database):
    _service, _facts, source, run_id = await seed(database)
    await record(database, source, run_id)
    old_key = hashlib.sha256(
        json.dumps(
            [run_id, "execution-1", "core", "terminal_exec", "call-1"],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    async with database.sessions() as session, session.begin():
        assert await session.scalar(select(MemoryToolReceiptModel.source_call_key)) == old_key
        await session.execute(update(CanonicalConversationModel).values(generation=2))
    with pytest.raises(MemoryPartitionResolutionError, match="tool_receipt_source_changed"):
        await record(database, source, run_id, call="call-2")
    async with database.sessions() as session:
        assert await session.scalar(select(func.count(MemoryToolReceiptModel.id))) == 1
