"""Actorless SELF evidence, read scope and restart-safe receipt reflection."""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from tests.conftest import make_settings
from tests.unit.test_memory_mutation import _event, _service

from qq_ai_bot.conversation.autonomy_db_models import AutonomyBindingModel, InitiativeRunModel
from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel
from qq_ai_bot.identity.db_models import SpaceBindingModel
from qq_ai_bot.llm.fake import FakeLLMProvider
from qq_ai_bot.mcp.repository import MCPRepository
from qq_ai_bot.memory.enums import MemoryKind, MemoryScopeType, SelfMemoryVisibility
from qq_ai_bot.memory.metrics import MemoryLifecycleMetrics
from qq_ai_bot.memory.mutation.models import (
    MemoryDecisionActorType,
    MemoryMutationContext,
    MemoryMutationOperation,
    MemoryMutationRequest,
    MemoryMutationTarget,
)
from qq_ai_bot.memory.runtime.partition_lookup import DatabaseMemoryPartitionLookup
from qq_ai_bot.memory.runtime.turn_session import TurnMemorySession
from qq_ai_bot.memory.self_reflection.repository import SelfReflectionRepository
from qq_ai_bot.memory.self_reflection.service import SelfReflectionService
from qq_ai_bot.memory.subjects import ResolvedSubject
from qq_ai_bot.model_runtime.executor import LegacyTaskModelExecutor
from qq_ai_bot.persistence.models import (
    ChatEventModel,
    MemoryMutationReceiptModel,
    MemorySelfReflectionRunModel,
    MemoryToolReceiptModel,
)


async def seed(database):
    service, facts, ledger, _processor = _service(database, self_memory_enabled=True)
    event = await _event(
        ledger,
        message_id=str(uuid4()),
        sender_user_id="1001",
        content="建立测试群身份",
        group_id="3001",
    )
    run_id = str(uuid4())
    now = datetime.now(UTC)
    async with database.sessions() as session, session.begin():
        conv = await session.get(CanonicalConversationModel, event.canonical_conversation_id)
        session.add(
            AutonomyBindingModel(
                conversation_id=conv.id,
                generation=conv.generation,
                master_enabled=True,
                external_enabled=True,
                effective_owner="semantic",
                controller_epoch=1,
                revision=1,
                updated_at=now,
            )
        )
        await session.flush()
        session.add(
            InitiativeRunModel(
                id=run_id,
                proposal_id=run_id,
                conversation_id=conv.id,
                generation=conv.generation,
                owner="semantic",
                controller_epoch=1,
                space_id=conv.space_id,
                presence_id=event.ingress_presence_id,
                target_person_id=event.author_person_id,
                payload_hash="0" * 64,
                sources_json="[]",
                support_refs_json="[]",
                state="running",
                feedback_sequence=0,
                created_at=now,
                updated_at=now,
            )
        )
    return service, facts, event, run_id


async def record(database, event, run_id, call="call-1", **extra):
    await MCPRepository(database).record_invocation(
        conversation_key=event.scope.key,
        provider_id="core",
        tool_name="terminal_exec",
        success=True,
        latency_seconds=0,
        result_size=10,
        artifact_created=False,
        error_category=None,
        initiative_run_id=run_id,
        execution_id="execution-1",
        tool_call_id=call,
        canonical_conversation_id=event.canonical_conversation_id,
        result_excerpt="已生成并校验曲线绘图",
        **extra,
    )


async def finish(database, run_id):
    async with database.sessions() as session, session.begin():
        run = await session.get(InitiativeRunModel, run_id)
        run.state = "no_reply"


async def claim(database, cycle="cycle-1"):
    return await SelfReflectionRepository(database).claim_due(
        scheduled_slot="synthetic",
        local_date="2026-09-22",
        event_threshold=20,
        character_threshold=16000,
        max_wait_seconds=3600,
        max_sessions=1,
        max_daily_calls=96,
        max_events=100,
        max_characters=16000,
        cycle_id=cycle,
    )


@pytest.mark.asyncio
async def test_run_receipt_xor_dedup_and_no_manufactured_events(database):
    _, _, event, run_id = await seed(database)
    await record(database, event, run_id)
    await record(database, event, run_id)
    with pytest.raises(ValueError, match="exclusive"):
        await record(database, event, run_id, trigger_event_id=event.id)
    async with database.sessions() as session:
        rows = list(await session.scalars(select(MemoryToolReceiptModel)))
        assert len(rows) == 1
        assert rows[0].trigger_event_id is None
        assert rows[0].initiative_run_id == run_id
        assert rows[0].canonical_person_id is None
        assert await session.scalar(select(func.count(ChatEventModel.id))) == 1


@pytest.mark.asyncio
async def test_committed_tool_receipt_does_not_need_a_live_group_binding(database):
    _, _, event, run_id = await seed(database)
    async with database.sessions() as session, session.begin():
        binding = await session.scalar(
            select(SpaceBindingModel).where(
                SpaceBindingModel.external_space_id == "3001",
            )
        )
        binding.status = "disabled"
    await record(database, event, run_id)
    async with database.sessions() as session:
        row = await session.scalar(select(MemoryToolReceiptModel))
        assert row.initiative_run_id == run_id and row.trigger_event_id is None


@pytest.mark.asyncio
async def test_actorless_prefetch_has_no_target_person_scope(database):
    _, _, event, run_id = await seed(database)
    turn = await TurnMemorySession.open_self_origin(
        initiative_run_id=run_id,
        canonical_conversation_id=event.canonical_conversation_id,
        identity=event.scope,
        runtime=SimpleNamespace(memory=SimpleNamespace(retrieval_enabled=True)),
        memory_context=SimpleNamespace(),
        partition_lookup=DatabaseMemoryPartitionLookup(database),
        user_question="看看群里共同的经历",
    )
    turn._query.read = AsyncMock(return_value=SimpleNamespace())
    await turn.prefetch()
    request = turn._query.read.call_args.args[1]
    assert {t.scope_type for t in request.resolved_scope.targets} == {
        MemoryScopeType.GROUP,
        MemoryScopeType.SELF,
    }
    assert all(
        t.subject_user_id is None and t.visibility_user_id is None
        for t in request.resolved_scope.targets
    )
    assert turn._inbound is None
    assert turn._source_key == f"initiative:{run_id}"


@pytest.mark.asyncio
async def test_silent_reflection_cursor_survives_restart_and_late_receipt(database):
    _, _, event, run_id = await seed(database)
    await record(database, event, run_id)
    await finish(database, run_id)
    (batch,) = await claim(database)
    assert batch.events == () and batch.initiative_run_id == run_id
    assert len(await SelfReflectionRepository(database).tool_receipts(batch)) == 1
    assert await claim(database, "cycle-2") == ()
    await SelfReflectionRepository(database).complete(batch, proposals=0, committed=0)
    assert await claim(database, "cycle-3") == ()
    await record(database, event, run_id, "call-2")
    (later,) = await claim(database, "cycle-4")
    assert later.first_receipt_id > batch.last_receipt_id
    assert later.run_id != batch.run_id


@pytest.mark.asyncio
async def test_actorless_reflection_uses_unified_mutation_and_readable_evidence(database):
    service, facts, event, run_id = await seed(database)
    await record(database, event, run_id)
    await finish(database, run_id)
    (batch,) = await claim(database)
    context = MemoryMutationContext(
        event=None,
        conversation_key="unused",
        turn_origin="memory_self_reflection",
        delegation_mode="self_reflection",
        trigger_actor_user_id="",
        executed_by_bot_user_id="",
        decision_actor_type=MemoryDecisionActorType.REFLECTION,
        decision_actor_id="yuki_self_reflection",
        initiative_run_id=run_id,
        evidence_tool_receipt_id=batch.first_receipt_id,
    )
    request = MemoryMutationRequest(
        operation=MemoryMutationOperation.CREATE,
        target=MemoryMutationTarget(subject_ref="self", scope_type=MemoryScopeType.SELF),
        new_content="我在群里独立完成了曲线绘图，并用真实运行结果校验了产物。",
        memory_key="self_episode:synthetic",
        category="self_episode",
        kind=MemoryKind.EPISODE,
        importance=4,
        reason="一次有真实工具结果支持的独立完成经历",
        evidence_quote="已生成并校验曲线绘图",
    )
    target = ResolvedSubject(
        MemoryScopeType.SELF, None, None, SelfMemoryVisibility.GROUP, None, "3001"
    )
    result = await service.mutate_resolved(
        request, context, target=target, self_reflection_result=(batch.run_id, "episode", 0)
    )
    assert result.ok, result.reason_code
    evidence = await facts.list_evidence(result.new_fact_id)
    assert len(evidence) == 1 and evidence[0].event_id is None
    assert evidence[0].tool_receipt_id == batch.first_receipt_id
    duplicate = await service.mutate_resolved(request, context, target=target)
    assert duplicate.deduplicated
    async with database.sessions() as session:
        receipt = await session.scalar(select(MemoryMutationReceiptModel))
        assert receipt.trigger_source_type == "initiative_run"
        assert receipt.initiative_run_id == run_id and receipt.trigger_event_id is None
    rejected = await service.mutate_resolved(
        request,
        context,
        target=ResolvedSubject(
            MemoryScopeType.SELF,
            None,
            None,
            SelfMemoryVisibility.PRIVATE,
            "1001",
            None,
        ),
    )
    assert not rejected.ok and rejected.reason_code == "initiative_memory_scope_forbidden"


@pytest.mark.asyncio
async def test_silent_service_episode_keeps_checkpoint_without_second_model_request(database):
    mutations, facts, event, run_id = await seed(database)
    await record(database, event, run_id)
    await finish(database, run_id)
    (batch,) = await claim(database)
    repository = SelfReflectionRepository(database)
    provider = FakeLLMProvider(
        responder=lambda _: json.dumps(
            {
                "proposals": [],
                "episodes": [
                    {
                        "passages": [
                            {
                                "evidence_refs": ["tool_1"],
                                "content": "我独立生成了曲线绘图，并校验了产物。",
                            }
                        ],
                        "value_reason": "有真实工具结果的一次自主完成经历",
                        "importance": 4,
                    }
                ],
            },
            ensure_ascii=False,
        )
    )
    reflection = SelfReflectionService(
        settings=make_settings("sqlite+aiosqlite:///:memory:", self_memory_enabled=True),
        repository=repository,
        facts=facts,
        mutations=mutations,
        models=LegacyTaskModelExecutor(provider),
        metrics=MemoryLifecycleMetrics(),
    )
    projected, _, _, event_map, _ = await reflection._input(batch)
    assert projected.source_kind == "initiative_tools" and not projected.events and not event_map
    assert projected.tool_receipts[0].occurred_at is not None
    assert await reflection.reflect(batch) == (1, 1)
    request_count = len(provider.requests)
    # A crash between committed result/checkpoint and cursor acknowledgement is recovered.
    assert await repository.recover_interrupted(batch.run_id, "stale_processing") == "completed"
    assert await reflection.reflect(batch) == (1, 1)
    assert len(provider.requests) == request_count
    assert await claim(database, "cycle-next") == ()


@pytest.mark.asyncio
async def test_receipt_window_failure_preserves_source_and_backoff(database):
    _, _, event, run_id = await seed(database)
    await record(database, event, run_id)
    await finish(database, run_id)
    (batch,) = await claim(database)
    repository = SelfReflectionRepository(database)
    assert await repository.recover_interrupted(batch.run_id, "synthetic_failure") == "failed"
    assert await claim(database, "cycle-2") == ()
    async with database.sessions() as session, session.begin():
        run = await session.get(MemorySelfReflectionRunModel, batch.run_id)
        run.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
        receipt = await session.get(MemoryToolReceiptModel, batch.first_receipt_id)
        receipt.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    assert await repository.cleanup_receipts() == 0
    (retried,) = await claim(database, "cycle-3")
    assert retried.run_id == batch.run_id and retried.first_receipt_id == batch.first_receipt_id
