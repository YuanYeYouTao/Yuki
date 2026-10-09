"""Actorless SELF evidence, read scope and restart-safe receipt reflection."""

import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from tests.conftest import make_settings
from tests.support.model_executor import InjectedModelExecutor
from tests.unit.test_memory_mutation import _event, _service

from qq_ai_bot.conversation.autonomy_db_models import AutonomyBindingModel, InitiativeRunModel
from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel
from qq_ai_bot.identity.db_models import SpaceBindingModel
from qq_ai_bot.llm.fake import FakeLLMProvider
from qq_ai_bot.memory.enums import MemoryKind, MemoryScopeType, MemoryStatus, SelfMemoryVisibility
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
from qq_ai_bot.memory.self_reflection.control import ReflectionControlRepository
from qq_ai_bot.memory.self_reflection.repository import SelfReflectionRepository
from qq_ai_bot.memory.self_reflection.service import SelfReflectionService
from qq_ai_bot.memory.subjects import ResolvedSubject
from qq_ai_bot.persistence.models import (
    ChatEventModel,
    MemoryEvidenceModel,
    MemoryMutationReceiptModel,
    MemorySelfReflectionRunModel,
    MemorySelfReflectionStateModel,
    MemoryToolReceiptModel,
)
from qq_ai_bot.tool_results.recorder import ToolInvocationRepository


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


async def record(
    database,
    event,
    run_id,
    call="call-1",
    *,
    result_excerpt="已生成并校验曲线绘图",
    reflection_excerpt_characters=2000,
    **extra,
):
    await ToolInvocationRepository(
        database, reflection_excerpt_characters=reflection_excerpt_characters
    ).record_invocation(
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
        result_excerpt=result_excerpt,
        **extra,
    )


async def finish(database, run_id):
    async with database.sessions() as session, session.begin():
        run = await session.get(InitiativeRunModel, run_id)
        run.state = "no_reply"


async def claim(database, cycle="cycle-1", *, max_characters=16000):
    return await SelfReflectionRepository(database).claim_due(
        scheduled_slot="synthetic",
        local_date="2026-09-22",
        event_threshold=20,
        character_threshold=16000,
        max_wait_seconds=3600,
        max_sessions=1,
        max_daily_calls=96,
        max_events=100,
        max_characters=max_characters,
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
async def test_actorless_memory_scope_has_no_target_person_partition(database):
    _, _, event, run_id = await seed(database)
    turn = await TurnMemorySession.open_self_origin(
        initiative_run_id=run_id,
        canonical_conversation_id=event.canonical_conversation_id,
        identity=event.scope,
        partition_lookup=DatabaseMemoryPartitionLookup(database),
    )
    assert turn.scope.scope_type.value == "group"
    assert turn.scope.scope_id == event.group_id
    assert turn.scope.partition_key == f"group:{event.group_id}"
    assert turn.contract.persistent_write_allowed


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
async def test_silent_receipt_windows_keep_large_source_complete_and_remaining_source_pending(
    database,
):
    mutations, facts, event, run_id = await seed(database)
    for call, excerpt in (("large", "甲" * 8000), ("small", "乙" * 1000)):
        await record(
            database,
            event,
            run_id,
            call,
            result_excerpt=excerpt,
            reflection_excerpt_characters=8000,
        )
    await finish(database, run_id)
    repository = SelfReflectionRepository(database)
    provider = FakeLLMProvider(responder=lambda _: '{"proposals":[],"episodes":[]}')
    reflection = SelfReflectionService(
        settings=make_settings(database.url),
        repository=repository,
        facts=facts,
        mutations=mutations,
        models=InjectedModelExecutor(provider),
        metrics=MemoryLifecycleMetrics(),
    )
    (first,) = await claim(database, "large-first", max_characters=4000)
    assert first.first_receipt_id == first.last_receipt_id
    assert [len(item.result_excerpt) for item in await repository.tool_receipts(first)] == [8000]
    assert await reflection.reflect(first) == (0, 0)
    first_payload = json.loads(provider.requests[0].messages[-1].content)
    assert [item["result_excerpt"] for item in first_payload["tool_receipts"]] == ["甲" * 8000]
    await repository.complete(first, proposals=0, committed=0)
    (second,) = await claim(database, "small-second", max_characters=4000)
    assert second.run_id != first.run_id
    assert second.first_receipt_id == second.last_receipt_id > first.last_receipt_id
    assert await reflection.reflect(second) == (0, 0)
    second_payload = json.loads(provider.requests[1].messages[-1].content)
    assert [item["result_excerpt"] for item in second_payload["tool_receipts"]] == ["乙" * 1000]
    await repository.complete(second, proposals=0, committed=0)
    assert await claim(database, "finished", max_characters=4000) == ()


@pytest.mark.asyncio
async def test_silent_original_receipt_window_retries_without_reselecting_new_budget(database):
    mutations, facts, event, run_id = await seed(database)
    for call, excerpt in (("first", "甲" * 1000), ("second", "乙" * 1000)):
        await record(database, event, run_id, call, result_excerpt=excerpt)
    await finish(database, run_id)
    repository = SelfReflectionRepository(database)
    (original,) = await claim(database, "original", max_characters=4000)
    async with database.sessions() as session:
        original_fingerprint = (
            await session.get(MemorySelfReflectionRunModel, original.run_id)
        ).input_fingerprint
    await repository.fail(original.run_id, "provider_unavailable")
    (retried,) = await claim(database, "changed-budget", max_characters=1000)
    assert retried.run_id == original.run_id
    assert (retried.first_receipt_id, retried.last_receipt_id) == (
        original.first_receipt_id,
        original.last_receipt_id,
    )
    async with database.sessions() as session:
        row = await session.get(MemorySelfReflectionRunModel, original.run_id)
        assert row.input_fingerprint == original_fingerprint and row.retry_state != "isolated"
    provider = FakeLLMProvider(responder=lambda _: '{"proposals":[],"episodes":[]}')
    reflection = SelfReflectionService(
        settings=make_settings(database.url),
        repository=repository,
        facts=facts,
        mutations=mutations,
        models=InjectedModelExecutor(provider),
        metrics=MemoryLifecycleMetrics(),
    )
    assert await reflection.reflect(retried) == (0, 0)
    payload = json.loads(provider.requests[0].messages[-1].content)
    assert [item["result_excerpt"] for item in payload["tool_receipts"]] == [
        "甲" * 1000,
        "乙" * 1000,
    ]
    assert await reflection.reflect(retried) == (0, 0)
    assert len(provider.requests) == 1
    await repository.complete(retried, proposals=0, committed=0)
    assert await claim(database, "finished", max_characters=1000) == ()


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
    metadata_request = request.model_copy(
        update={
            "operation": MemoryMutationOperation.UPDATE_METADATA,
            "fact_id": result.new_fact_id,
            "target": None,
            "new_content": None,
            "category": "experience_note",
            "importance": 5,
        }
    )
    metadata = await service.mutate_resolved(metadata_request, context, target=target)
    assert metadata.ok, metadata.reason_code
    updated = await facts.get_fact(metadata.new_fact_id)
    assert updated.supersedes_id == result.new_fact_id
    assert updated.category == "experience_note" and updated.importance == 5
    assert updated.content == request.new_content
    assert (await facts.list_evidence(updated.id))[0].tool_receipt_id == batch.first_receipt_id
    assert (await service.mutate_resolved(metadata_request, context, target=target)).deduplicated
    invalidate_request = metadata_request.model_copy(
        update={"operation": MemoryMutationOperation.INVALIDATE, "fact_id": updated.id}
    )
    assert (await service.mutate_resolved(invalidate_request, context, target=target)).ok
    assert (await service.mutate_resolved(invalidate_request, context, target=target)).deduplicated
    assert (await facts.get_fact(updated.id)).status is MemoryStatus.INVALIDATED
    restore_request = invalidate_request.model_copy(
        update={"operation": MemoryMutationOperation.RESTORE}
    )
    restored = await service.mutate_resolved(restore_request, context, target=target)
    assert restored.ok, restored.reason_code
    assert restored.new_fact_id == updated.id
    assert (await facts.get_fact(updated.id)).status is MemoryStatus.ACTIVE
    assert (await service.mutate_resolved(restore_request, context, target=target)).deduplicated
    bad_quote = restore_request.model_copy(update={"evidence_quote": "不存在的工具结果"})
    assert (
        await service.mutate_resolved(bad_quote, context, target=target)
    ).reason_code == "evidence_quote_not_in_tool_receipt"
    async with database.sessions() as session:
        receipts = list(await session.scalars(select(MemoryMutationReceiptModel)))
        assert len(receipts) == 4
        assert all(
            item.initiative_run_id == run_id and item.trigger_event_id is None for item in receipts
        )
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
                "proposals": [{"operation": "noop", "evidence_refs": ["tool_1"]}],
                "episodes": [
                    {
                        "passages": [
                            {
                                "evidence_refs": ["tool_1", "tool_1"],
                                "content": "我独立生成了曲线绘图，并校验了产物。",
                            }
                        ],
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
        models=InjectedModelExecutor(provider),
        metrics=MemoryLifecycleMetrics(),
    )
    projected, _, _, event_map, _ = await reflection._input(batch)
    assert projected.source_kind == "initiative_tools" and not projected.events and not event_map
    assert projected.tool_receipts[0].occurred_at is not None
    assert await reflection.reflect(batch) == (2, 1)
    async with database.sessions() as session:
        assert await session.scalar(select(func.count(MemoryEvidenceModel.id))) == 1
    request_count = len(provider.requests)
    # A crash between committed result/checkpoint and cursor acknowledgement is recovered.
    assert await repository.recover_interrupted(batch.run_id, "stale_processing") == "completed"
    assert await reflection.reflect(batch) == (2, 1)
    assert len(provider.requests) == request_count
    assert await claim(database, "cycle-next") == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("multiple_bindings", [False, True])
async def test_main_self_memory_write_uses_original_tool_receipt_without_human_event(
    database, tmp_path, multiple_bindings
):
    from tests.conftest import build_harness
    from tests.integration.test_self_automation_delivery import self_actor
    from tests.support.social_identity_cases import social_env

    from qq_ai_bot.conversation.scope import ConversationTurnSnapshot
    from qq_ai_bot.domain.conversations import ConversationScope, ScopeType
    from qq_ai_bot.domain.messages import ChatResponse, ToolCall, ToolFunction
    from qq_ai_bot.runtime.origin import TurnOrigin
    from qq_ai_bot.runtime.work_activation import bind_work_activation
    from qq_ai_bot.runtime.work_control import WorkControl
    from qq_ai_bot.runtime.work_repository import WorkRepository
    from qq_ai_bot.services.agent_tools import ToolRuntime
    from qq_ai_bot.services.execution_sources import recover_self_source
    from qq_ai_bot.services.main_agent_contract import MainAgentContract
    from qq_ai_bot.workspace.short_state import ShortState

    env = await social_env(database, tmp_path)
    async with database.sessions() as session:
        binding_id = await session.scalar(
            select(SpaceBindingModel.id).where(SpaceBindingModel.external_space_id == "20001")
        )
    if multiple_bindings:
        async with database.sessions() as session, session.begin():
            session.add(
                SpaceBindingModel(
                    id=str(uuid4()),
                    space_id=env.space,
                    platform="qq",
                    external_space_id="20002",
                    first_seen_at=datetime.now(UTC),
                    last_seen_at=datetime.now(UTC),
                    created_at=datetime.now(UTC),
                    updated_at=datetime.now(UTC),
                )
            )
    actor = await self_actor(database, env)
    mutations, facts, _ledger, _ = _service(database, self_memory_enabled=True)
    write = {
        "operation": "create",
        "target": {"subject_ref": "self", "scope_type": "self"},
        "new_content": "我实际查询了当前群成员列表，确认了 member 的成员资料。",
        "memory_key": "self_episode:actual-tool",
        "category": "self_episode",
        "kind": "episode",
        "evidence_quote": "member",
    }
    calls = [
        ("get_group_members", {"limit": 10, "space_binding_id": binding_id}),
        ("get_group_members", {"limit": 20, "space_binding_id": binding_id}),
        ("memory_change", write),
    ]
    results = []

    def respond(request):
        if len(requests := provider.requests) > 1:
            results.append(request.messages[-1].content)
        index = len(requests) - 1
        if index >= len(calls):
            return "NO_REPLY"
        name, arguments = calls[index]
        return ChatResponse(
            "",
            0,
            tool_calls=(
                ToolCall(
                    f"original-{index}",
                    ToolFunction(name, json.dumps(arguments, ensure_ascii=False)),
                ),
            ),
        )

    provider = FakeLLMProvider(responder=respond)
    harness = build_harness(
        database,
        make_settings(
            database.url,
            enabled_groups_csv="20001",
            runtime_work_enabled=True,
            self_memory_enabled=True,
        ),
        provider,
    )
    chat = harness.processor._chat
    chat._tools.social_service = env.service
    chat._tools._memory_mutations = mutations
    chat._tool_invocations = ToolInvocationRepository(database)
    chat.runtime.runner.main_contract = MainAgentContract(chat, ShortState(env.store))
    owner = WorkRepository(database)
    source = {
        "origin": "self_initiative",
        "principal_kind": "self",
        "actor_user_id": "",
        "initiative_run_id": actor.initiative_run_id,
        "conversation_id": actor.conversation_id,
        "generation": 1,
        "space_id": env.space,
        "presence_id": env.presence,
        "bot_user_id": "80001",
        "group_id": "20001",
        "instruction": "记录实际工具经历，然后静默结束",
        "delivery_contract": "return_to_caller",
    }
    lease = await owner.acquire(actor.conversation_id, 1)
    item = await owner.accept(
        lease, source_key=actor.source_key, source=source, goal=source["instruction"]
    )
    recovered = await recover_self_source(
        database, actor.conversation_id, source, request_id=item["id"]
    )
    identity = ConversationScope.group("80001", "20001")

    async def validate():
        assert await owner.valid(lease)

    control = WorkControl(owner, lease, item["source_key"], source, validate)
    control.current = item
    async with chat._turn_coordinator.background_turn(identity.key) as token:
        snapshot = ConversationTurnSnapshot(
            actor.conversation_id,
            identity.key,
            1,
            None,
            token.version,
            identity.key,
            initiative_run_id=actor.initiative_run_id,
        )
        async with bind_work_activation(control):
            result = await chat.generate_self_initiative(
                trigger=recovered.trigger(),
                runtime=await chat._runtime_config.snapshot(group_id="20001"),
                turn_token=token,
                turn_snapshot=snapshot,
                before_model_request=validate,
                source_runtime=ToolRuntime(
                    inbound=None,
                    actor_context=recovered.actor(item["id"]),
                    gateway=None,
                    allow_generic_onebot=False,
                    conversation_key=identity.key,
                    execution_id=item["id"],
                    origin=TurnOrigin.SELF_INITIATIVE,
                    initiative_run_id=actor.initiative_run_id,
                    conversation_id=actor.conversation_id,
                    scope_type=ScopeType.GROUP,
                    external_target_id="20001",
                    space_id=env.space,
                    sandbox_source={**source, "work_id": item["id"]},
                    allow_work_environment=True,
                ),
            )
    assert result.text == "" and len(provider.requests) == 4, results
    assert all(json.loads(text).get("ok") for text in results), results[1]
    async with database.sessions() as session:
        mutation = await session.scalar(select(MemoryMutationReceiptModel))
        assert mutation is not None, results
        assert await session.scalar(select(func.count(MemoryMutationReceiptModel.id))) == 1
        assert (
            mutation.initiative_run_id == actor.initiative_run_id
            and mutation.trigger_event_id is None
        )
        receipts = list(
            await session.scalars(
                select(MemoryToolReceiptModel)
                .where(MemoryToolReceiptModel.tool_name == "get_group_members")
                .order_by(MemoryToolReceiptModel.id)
            )
        )
        assert len(receipts) == 2
        receipt = receipts[-1]
        assert (
            receipt.initiative_run_id == actor.initiative_run_id
            and receipt.execution_id == item["id"]
        )
        assert await session.scalar(select(func.count(ChatEventModel.id))) == 1
    fact = await facts.get_fact(mutation.new_fact_id)
    evidence = await facts.list_evidence(fact.id)
    assert fact.scope_type is MemoryScopeType.SELF and fact.visibility_group_id == "20001"
    assert (
        len(evidence) == 1
        and evidence[0].tool_receipt_id == receipt.id
        and evidence[0].event_id is None
    )
    assert (await owner.get(item["id"]))["state"] == "completed"
    assert not await owner.valid(lease)
    assert not any(name.startswith("send_") for name, _ in env.bot.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["restore", "update_metadata"])
async def test_reflection_model_reuses_restore_and_metadata_domain_operations(database, operation):
    mutations, facts, event, run_id = await seed(database)
    await record(database, event, run_id)
    async with database.sessions() as session:
        receipt = await session.scalar(select(MemoryToolReceiptModel))
    context = MemoryMutationContext(
        event=None,
        conversation_key="self-test",
        turn_origin="memory_self_reflection",
        delegation_mode="self_reflection",
        trigger_actor_user_id="",
        executed_by_bot_user_id="",
        decision_actor_type=MemoryDecisionActorType.REFLECTION,
        decision_actor_id="yuki_self_reflection",
        initiative_run_id=run_id,
        evidence_tool_receipt_id=receipt.id,
    )
    target = ResolvedSubject(
        MemoryScopeType.SELF, None, None, SelfMemoryVisibility.GROUP, None, "3001"
    )
    request = MemoryMutationRequest(
        operation=MemoryMutationOperation.CREATE,
        target=MemoryMutationTarget(subject_ref="self", scope_type=MemoryScopeType.SELF),
        new_content="我用运行结果校验了曲线绘图。",
        memory_key="self_fact:existing",
        category="self_fact",
        kind=MemoryKind.FACT,
        evidence_quote="已生成并校验曲线绘图",
    )
    created = await mutations.mutate_resolved(request, context, target=target)
    assert created.ok
    if operation == "restore":
        invalidated = await mutations.mutate_resolved(
            request.model_copy(
                update={
                    "operation": MemoryMutationOperation.INVALIDATE,
                    "fact_id": created.new_fact_id,
                    "target": None,
                    "new_content": None,
                }
            ),
            context,
            target=target,
        )
        assert invalidated.ok
    await finish(database, run_id)
    (batch,) = await claim(database)

    def respond(request):
        payload = json.loads(request.messages[-1].content)
        fact = next(
            item for item in payload["self_facts"] if item["memory_key"] == "self_fact:existing"
        )
        if operation == "restore":
            assert fact["status"] == "invalidated"
        return json.dumps(
            {
                "proposals": [
                    {
                        "operation": operation,
                        "fact_ref": fact["ref"],
                        "evidence_refs": ["tool_1"],
                        "reason": "实际工具回执支持本次维护",
                        "category": "self_reflection",
                        "importance": 5,
                        "confidence": 0.9,
                    }
                ]
            }
        )

    provider = FakeLLMProvider(responder=respond)
    repository = SelfReflectionRepository(database)
    reflection = SelfReflectionService(
        settings=make_settings(database.url, self_memory_enabled=True),
        repository=repository,
        facts=facts,
        mutations=mutations,
        models=InjectedModelExecutor(provider),
        metrics=MemoryLifecycleMetrics(),
    )
    assert await reflection.reflect(batch) == (1, 1)
    assert await reflection.reflect(batch) == (1, 1)
    assert len(provider.requests) == 1
    async with database.sessions() as session:
        mutation = await session.scalar(
            select(MemoryMutationReceiptModel).where(
                MemoryMutationReceiptModel.requested_operation == operation
            )
        )
        assert (
            mutation is not None
            and mutation.trigger_event_id is None
            and mutation.initiative_run_id == run_id
        )
        assert (
            await session.scalar(
                select(func.count(MemoryMutationReceiptModel.id)).where(
                    MemoryMutationReceiptModel.requested_operation == operation
                )
            )
            == 1
        )
        assert await session.scalar(select(func.count(ChatEventModel.id))) == 1
    result = await facts.get_fact(mutation.new_fact_id)
    assert result.status is MemoryStatus.ACTIVE
    if operation == "restore":
        assert result.id == created.new_fact_id
    else:
        assert result.supersedes_id == created.new_fact_id
        assert result.category == "self_reflection" and result.importance == 5
    assert all(
        item.tool_receipt_id == receipt.id and item.event_id is None
        for item in await facts.list_evidence(result.id)
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit_refs", [False, True])
async def test_main_self_evidence_uses_actual_receipt_with_repeated_quote_and_partial_refs(
    database, explicit_refs
):
    mutations, facts, event, run_id = await seed(database)
    await record(database, event, run_id, "first", result_excerpt="同一句实际工具结果")
    await record(database, event, run_id, "second", result_excerpt="同一句实际工具结果")
    await record(database, event, run_id, "third", result_excerpt="另一项未使用资料")
    async with database.sessions() as session:
        receipts = list(
            await session.scalars(
                select(MemoryToolReceiptModel).order_by(MemoryToolReceiptModel.id)
            )
        )
    context = MemoryMutationContext(
        event=None,
        conversation_key=event.scope.key,
        turn_origin="self_initiative",
        delegation_mode="main_agent",
        trigger_actor_user_id="",
        decision_actor_type=MemoryDecisionActorType.AGENT,
        decision_actor_id="execution-1",
        executed_by_bot_user_id=event.bot_user_id,
        initiative_run_id=run_id,
    )
    request = MemoryMutationRequest(
        operation=MemoryMutationOperation.CREATE,
        target=MemoryMutationTarget(subject_ref="self", scope_type=MemoryScopeType.SELF),
        new_content="我根据真实工具结果继续了自主工作。",
        memory_key="self_episode:repeated-receipts",
        category="self_episode",
        kind=MemoryKind.EPISODE,
        evidence_quote="同一句实际工具结果",
        evidence_refs=tuple(f"tool_{row.id}" for row in receipts) if explicit_refs else (),
    )
    result = await mutations.mutate(request, context)
    assert result.ok, result.reason_code
    evidence = await facts.list_evidence(result.new_fact_id)
    assert len(evidence) == 1 and evidence[0].tool_receipt_id == receipts[1].id
    assert evidence[0].event_id is None
    duplicate = await mutations.mutate(request, context)
    assert duplicate.deduplicated and duplicate.new_fact_id == result.new_fact_id
    missing_source = await mutations.mutate(
        request.model_copy(update={"evidence_refs": ("tool_999999",)}), context
    )
    assert (
        not missing_source.ok and missing_source.reason_code == "initiative_tool_evidence_required"
    )
    async with database.sessions() as session:
        assert await session.scalar(select(func.count(MemoryMutationReceiptModel.id))) == 1
        assert await session.scalar(select(func.count(ChatEventModel.id))) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("large_event", [False, True])
async def test_incoming_reflection_keeps_complete_source_without_own_reply(database, large_event):
    mutations, facts, _event_record, _ = await seed(database)
    repository = SelfReflectionRepository(database)
    await repository.scan_new_events()
    content = "我觉得你很会倾听。" + ("这是一段完整反馈。" * 100 if large_event else "")
    source = await _event(
        mutations._ledger,
        message_id=str(uuid4()),
        sender_user_id="1001",
        content=content,
        group_id="3001",
    )
    assert await repository.scan_new_events() == 1
    settings = make_settings(
        "sqlite+aiosqlite:///:memory:",
        self_memory_enabled=True,
        memory_self_reflection_event_threshold=1,
    )
    snapshot = await ReflectionControlRepository(database, settings).snapshot()
    assert snapshot["actionable"]["events"] == 1
    (batch,) = await repository.claim_due(
        scheduled_slot="incoming",
        local_date="2026-10-10",
        event_threshold=1,
        character_threshold=100,
        max_wait_seconds=0,
        max_sessions=1,
        max_daily_calls=96,
        max_events=100,
        max_characters=100,
        cycle_id="incoming",
    )
    assert not batch.state.has_yuki_reply and not batch.state.has_tool_result
    assert [item.id for item in batch.events] == [source.id]
    provider = FakeLLMProvider(responder=lambda _: '{"proposals":[],"episodes":[]}')
    reflection = SelfReflectionService(
        settings=settings,
        repository=repository,
        facts=facts,
        mutations=mutations,
        models=InjectedModelExecutor(provider),
        metrics=MemoryLifecycleMetrics(),
    )
    assert await reflection.reflect(batch) == (0, 0)
    payload = json.loads(provider.requests[0].messages[-1].content)
    assert len(payload["events"]) == 1
    assert content in payload["events"][0]["rendered"]
    assert await reflection.reflect(batch) == (0, 0)
    assert len(provider.requests) == 1
    await repository.complete(batch, proposals=0, committed=0)
    assert await repository.completed_result(batch.run_id) == (0, 0)
    assert (await ReflectionControlRepository(database, settings).snapshot())["actionable"][
        "events"
    ] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "source_state",
    ["old_waiting", "old_isolated", "live_waiting", "live_isolated", "cross_generation"],
)
async def test_reflection_only_live_source_ranges_block_claim_and_cursor(database, source_state):
    mutations, _, initial, _ = await seed(database)
    repository = SelfReflectionRepository(database)
    await repository.scan_new_events()
    old = await _event(
        mutations._ledger,
        message_id=str(uuid4()),
        sender_user_id="1001",
        content="旧范围反馈",
        group_id="3001",
    )
    crossing = None
    if source_state == "cross_generation":
        crossing = await _event(
            mutations._ledger,
            message_id=str(uuid4()),
            sender_user_id="1001",
            content="跨边界反馈",
            group_id="3001",
        )
    await repository.scan_new_events()
    options = dict(
        scheduled_slot="source-range",
        local_date="2026-10-10",
        event_threshold=1,
        character_threshold=100,
        max_wait_seconds=0,
        max_sessions=1,
        max_daily_calls=96,
        max_events=100,
        max_characters=16000,
    )
    (original,) = await repository.claim_due(**options, cycle_id="before-boundary")
    await repository.fail(original.run_id, "provider_unavailable")
    async with database.sessions() as session, session.begin():
        run = await session.get(MemorySelfReflectionRunModel, original.run_id)
        run.retry_state = "isolated" if source_state.endswith("isolated") else "waiting"
        run.next_attempt_at = (
            datetime.now(UTC) + timedelta(days=1) if source_state.startswith("live_") else None
        )
        fingerprint = run.input_fingerprint
        if not source_state.startswith("live_"):
            conversation = await session.get(
                CanonicalConversationModel, old.canonical_conversation_id
            )
            conversation.last_generation_change_event_id = old.id
    current = await _event(
        mutations._ledger,
        message_id=str(uuid4()),
        sender_user_id="1001",
        content="当前范围反馈",
        group_id="3001",
    )
    await repository.scan_new_events()
    options["scheduled_slot"] = "after-boundary"
    claimed = await repository.claim_due(**options, cycle_id="after-boundary")
    if crossing is not None:
        assert claimed == ()
    else:
        (batch,) = claimed
        assert [item.id for item in batch.events] == [current.id]
        await repository.complete(batch, proposals=0, committed=0)
        assert await repository.claim_due(**options, cycle_id="after-completion") == ()
    async with database.sessions() as session:
        run = await session.get(MemorySelfReflectionRunModel, original.run_id)
        state = await session.get(MemorySelfReflectionStateModel, original.state.id)
        assert run.status == "failed" and run.input_fingerprint == fingerprint
        assert run.first_event_id == old.id
        assert run.last_event_id == (crossing.id if crossing else old.id)
        if source_state.startswith("live_"):
            assert state.last_event_id == initial.id and state.pending_events == 1
        elif crossing is not None:
            assert run.retry_state == "isolated" and run.error_category == "source_range_changed"
            assert state.last_event_id == initial.id
        else:
            assert state.last_event_id == current.id and state.pending_events == 0
