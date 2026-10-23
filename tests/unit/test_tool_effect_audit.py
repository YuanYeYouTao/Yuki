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
from tests.support.canonical_identity import ensure_person, ensure_space
from tests.support.social_identity_cases import social_env
from tests.support.work_session import WorkSession, invoke_tool
from tests.unit.test_self_initiative_memory import record, seed

from yuki.capabilities import (
    CapabilityTrustSource,
    InProcessToolProvider,
    ToolProviderRegistry,
)
from yuki.capabilities.coordinator import ToolInvocationCoordinator
from yuki.capabilities.invocation import current_invocation
from yuki.capabilities.results import ToolExecutionResult
from yuki.conversation.canonical_db_models import (
    CanonicalConversationModel,
    SpaceActiveRouteModel,
    SpaceBindingIngestRouteModel,
)
from yuki.domain.messages import ChatMessage, ChatTool, ToolCall, ToolFunction
from yuki.domain.tool_actor import ToolActor
from yuki.execution_trace.db_models import ExecutionTraceStateModel
from yuki.identity.db_models import SpaceBindingModel
from yuki.memory.partition import MemoryPartitionResolutionError
from yuki.persistence.models import ChatEventModel, MemoryToolReceiptModel
from yuki.persistence.people_repository import PeopleRepository
from yuki.runtime.origin import TurnOrigin
from yuki.runtime.work_control import WorkControl
from yuki.runtime.work_repository import WorkRepository
from yuki.runtime.work_schema_v1 import effects
from yuki.services.agent_runner import AgentRuntime
from yuki.services.agent_tools import ToolRuntime
from yuki.services.chat import ChatService
from yuki.services.invocation_service import defer_tool_audit
from yuki.services.main_agent_backend import MainAgentBackend
from yuki.services.turn_transcript import TurnTranscript
from yuki.social.db_models import SocialOperationModel
from yuki.tool_results.recorder import ToolReceiptRepository


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
    async with database.read_session() as session:
        source = await session.scalar(select(ChatEventModel))
    runtime = ToolRuntime(
        inbound=None,
        conversation_key="audit",
        trigger_event_id=source.id,
        actor_context=ToolActor(
            user_id="10001",
            bot_user_id=env.bot.self_id,
            group_id="20001",
            origin=TurnOrigin.USER_MESSAGE,
            instruction=source.content,
            event_id=source.id,
            execution_id=f"event:{source.id}",
            person_id=env.person,
            conversation_id=env.context.conversation_id,
            presence_id=env.presence,
        ),
        conversation_id=env.context.conversation_id,
        execution_id=f"event:{source.id}",
    )
    return env, work, runtime


async def test_equal_sends_and_cross_response_ids_keep_distinct_social_operations(
    database, tmp_path
):
    env, owner, runtime = await active_work(database, tmp_path)
    chat = build_harness(database, make_settings(database.url)).processor._chat
    runtime = replace(
        runtime, runtime_config=await chat._runtime_config.snapshot(), space_id=env.space
    )

    async def dispatch(_name, _arguments, _runtime):
        context = current_invocation.get()
        assert context is not None
        result = await env.service.execute(
            "send",
            {"target": {"kind": "space", "target_id": env.space}, "text": "same"},
            replace(
                env.context,
                call_id=context.invocation_key,
                trigger_event_id=runtime.trigger_event_id,
            ),
        )
        return ToolExecutionResult(ok=result["status"] == "succeeded", data=result)

    registry = ToolProviderRegistry()
    registry.register(
        InProcessToolProvider(
            provider_id="core",
            source=CapabilityTrustSource.CORE,
            definitions=lambda _: (ChatTool("send", "send", {"type": "object"}),),
            execute=dispatch,
        )
    )
    backend = MainAgentBackend(chat, runtime)
    backend._catalog = registry.catalog(runtime)
    backend._callable_tool_names = {"send"}
    actor = runtime.actor_context
    agent = AgentRuntime(
        origin=actor.origin,
        actor_user_id=actor.user_id,
        actor_is_superuser=runtime.actor_is_superuser,
        conversation_key=runtime.conversation_key,
        current_group_id=actor.group_id,
        bot_user_id=actor.bot_user_id,
        gateway=None,
        runtime_config=runtime.runtime_config,
        allowed_capabilities=frozenset(),
        max_tool_calls=runtime.runtime_config.agent.max_tool_calls,
        max_model_requests=runtime.runtime_config.agent.max_model_requests,
        work_control=owner.control,
    )
    calls = tuple(ToolCall(i, ToolFunction("send", "{}")) for i in ("call_0", "call_1"))
    coordinator = ToolInvocationCoordinator()

    async def batch(selected):
        return await coordinator.execute_batch(
            selected,
            backend,
            agent,
            remaining_calls=10,
            max_parallel_calls=2,
            manifest_revision="contract",
        )

    first = await batch(calls)
    assert all(json.loads(value)["data"]["status"] == "succeeded" for _, value, _ in first.calls)
    await batch(calls)  # Reentry queries both original receipts.
    owner.sequence += 1
    await batch(calls[:1])  # Same Provider ID in a later model response.
    assert len([entry for entry in env.bot.calls if entry[0] == "send_group_msg"]) == 3
    async with database.read_session() as reader:
        original_ids = set(await reader.scalars(select(SocialOperationModel.tool_call_id)))
        assert original_ids == {
            f"{owner.transcript.chain_id}:0:call_0",
            f"{owner.transcript.chain_id}:0:call_1",
            f"{owner.transcript.chain_id}:1:call_0",
        }
        assert await reader.scalar(select(func.count()).select_from(effects)) == 3


async def invoke_audit(recorder, runtime, call_key, result='{"ok":true}'):
    await ChatService._record_tool_receipt(
        SimpleNamespace(_tool_receipts=recorder),
        runtime=runtime,
        provider_id="social",
        tool_name="send",
        success=True,
        error_category=None,
        result_excerpt=result,
        tool_call_id=call_key,
    )


@pytest.mark.parametrize("mode", ["cancel_audit", "uncertain_accepted_commit"])
async def test_post_effect_cancel_or_uncertain_commit_never_runs_unconfirmed_audit(
    database, tmp_path, monkeypatch, mode
):
    _env, work, runtime = await active_work(database, tmp_path)
    call = ToolCall("original", ToolFunction("send", "{}"))
    audit = AsyncMock(side_effect=asyncio.CancelledError() if mode == "cancel_audit" else None)
    recorder = SimpleNamespace(record_receipt=audit)
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
        await invoke_tool(work, call, invoke)
    async with database.read_session() as session:
        assert await session.scalar(select(effects.c.state)) == "accepted"
    assert audit.await_count == (1 if mode == "cancel_audit" else 0)
    assert not defer_tool_audit(work.call_key(call.id), AsyncMock())


async def test_independent_sdk_audit_identity_error_still_propagates(database, tmp_path):
    _env, _work, runtime = await active_work(database, tmp_path)
    recorder = SimpleNamespace(record_receipt=AsyncMock(side_effect=ValueError("source")))
    with pytest.raises(ValueError, match="source"):
        await invoke_audit(recorder, runtime, "sdk-call")
    recorder.record_receipt.assert_awaited_once()


@pytest.mark.parametrize("metadata_only", [False, True])
async def test_deferred_audit_cannot_refill_person_erased_before_accepted(
    database, tmp_path, monkeypatch, metadata_only
):
    _env, work, runtime = await active_work(database, tmp_path)
    async with database.immediate_session() as session:
        await ensure_person(session, "10002")
    source_id = runtime.trigger_event_id
    if metadata_only:
        runtime = replace(runtime, trigger_event_id=None)
    recorder = ToolReceiptRepository(database)
    call = ToolCall("original-erased-call", ToolFunction("send", "{}"))
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

    result = await invoke_tool(work, call, invoke)
    assert result == "confirmed" and dispatched == 1
    assert await work.journal.effect_result(work.call_key(call.id)) == result
    async with database.read_session() as session:
        assert await session.scalar(select(effects.c.state)) == "accepted"
        assert await session.get(ChatEventModel, source_id) is not None
        assert await session.scalar(select(ExecutionTraceStateModel.privacy_generation)) == 1
        assert await session.scalar(select(func.count(MemoryToolReceiptModel.id))) == 0


@pytest.mark.parametrize("after_prepare", [False, True])
async def test_group_binding_move_preserves_original_event_receipt_owner(
    database, tmp_path, monkeypatch, after_prepare
):
    env, work, runtime = await active_work(database, tmp_path)
    recorder = ToolReceiptRepository(database)
    call = ToolCall("original-owner-call", ToolFunction("send", "{}"))
    sessions = database.read_session
    destination = None

    async def move_binding():
        nonlocal destination
        async with database.immediate_session() as session:
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
        record_receipt = recorder.record_receipt

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

            monkeypatch.setattr(database, "read_session", moved_after_prepare)
            try:
                await record_receipt(**kwargs)
            finally:
                monkeypatch.setattr(database, "read_session", sessions)

        monkeypatch.setattr(recorder, "record_receipt", record_after_move)

    async def invoke():
        await invoke_audit(recorder, runtime, work.call_key(call.id))
        if not after_prepare:
            await move_binding()
        return "confirmed original result"

    result = await invoke_tool(work, call, invoke)
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
        receipt = (await session.scalars(select(MemoryToolReceiptModel))).one()
        assert receipt.canonical_space_id == env.space
        assert receipt.canonical_person_id is None
        assert receipt.trigger_event_id == runtime.trigger_event_id


@pytest.mark.parametrize("change", ["privacy", "binding_disabled", "generation"])
async def test_event_receipt_source_rechecked_atomically_after_read_prepare(
    database, tmp_path, monkeypatch, change
):
    env = await social_env(database, tmp_path)
    async with database.read_session() as session:
        source = await session.scalar(select(ChatEventModel))
    original = database.read_session
    first = True

    @asynccontextmanager
    async def changed_after_prepare():
        nonlocal first
        is_prepare = first
        first = False
        async with original() as session:
            yield session
        if is_prepare:
            async with database.immediate_session() as session:
                if change == "privacy":
                    session.add(ExecutionTraceStateModel(id=1, privacy_generation=1))
                elif change == "binding_disabled":
                    await session.execute(update(SpaceBindingModel).values(status="disabled"))
                else:
                    await session.execute(update(CanonicalConversationModel).values(generation=2))

    monkeypatch.setattr(database, "read_session", changed_after_prepare)

    async def record_original_receipt():
        await ToolReceiptRepository(database).record_receipt(
            provider_id="core",
            tool_name="terminal_exec",
            success=True,
            error_category=None,
            trigger_event_id=source.id,
            canonical_conversation_id=env.context.conversation_id,
            tool_call_id="original-call",
            execution_id="execution-1",
            bot_user_id=env.bot.self_id,
            result_excerpt="verified source",
        )

    if change == "binding_disabled":
        await record_original_receipt()
    else:
        with pytest.raises(MemoryPartitionResolutionError, match="tool_receipt_source_changed"):
            await record_original_receipt()
    async with original() as session:
        if change == "binding_disabled":
            receipt = (await session.scalars(select(MemoryToolReceiptModel))).one()
            assert receipt.canonical_space_id == env.space
            assert receipt.canonical_person_id is None
            assert receipt.trigger_event_id == source.id
        else:
            assert await session.scalar(select(func.count(MemoryToolReceiptModel.id))) == 0


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
    async with database.immediate_session() as session:
        assert await session.scalar(select(MemoryToolReceiptModel.source_call_key)) == old_key
        await session.execute(update(CanonicalConversationModel).values(generation=2))
    with pytest.raises(MemoryPartitionResolutionError, match="tool_receipt_source_changed"):
        await record(database, source, run_id, call="call-2")
    async with database.read_session() as session:
        assert await session.scalar(select(func.count(MemoryToolReceiptModel.id))) == 1
