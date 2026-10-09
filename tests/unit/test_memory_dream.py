"""Memory Dream partition, mutation, provenance, and rollback contracts."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import func, select
from tests.conftest import make_settings
from tests.support.model_executor import InjectedModelExecutor

from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.llm.fake import FakeLLMProvider
from qq_ai_bot.memory.claim_processor import MemoryClaimProcessor
from qq_ai_bot.memory.dream.db_models import (
    MemoryDreamOperationModel,
    MemoryDreamOperationResultModel,
)
from qq_ai_bot.memory.dream.models import (
    DreamAction,
    DreamOperationStatus,
    DreamOperationType,
    DreamPlanStatistics,
    DreamRecomposeOutput,
    DreamRunMode,
)
from qq_ai_bot.memory.dream.repository import (
    DreamRepository,
    fact_signature,
)
from qq_ai_bot.memory.dream.service import DreamService
from qq_ai_bot.memory.enums import (
    MemoryAuthority,
    MemoryConflictState,
    MemoryEvidenceRelation,
    MemoryFactRelationType,
    MemoryKind,
    MemoryScopeType,
    MemorySourceType,
    MemoryStatus,
)
from qq_ai_bot.memory.models import (
    MemoryEvidenceCreate,
    MemoryFact,
    MemoryFactCreate,
)
from qq_ai_bot.memory.mutation.service import DreamRecomposePlan, MemoryMutationService
from qq_ai_bot.memory.repository import MemoryFactRepository
from qq_ai_bot.memory.service import MemoryFactService
from qq_ai_bot.model_runtime.structured import StructuredTaskRunner
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.models import (
    MemoryEvidenceModel,
    MemoryFactRelationModel,
    MemoryMutationReceiptModel,
)
from qq_ai_bot.persistence.repositories import EventLedgerRepository, PeopleRepository
from qq_ai_bot.services.concurrency import ConcurrencyManager


def _services(
    database: Database,
) -> tuple[MemoryMutationService, MemoryFactService, EventLedgerRepository, DreamRepository]:
    settings = make_settings(database.url)
    facts = MemoryFactService(MemoryFactRepository(database))
    ledger = EventLedgerRepository(database)
    processor = MemoryClaimProcessor(
        settings=settings,
        facts=facts,
    )
    return (
        MemoryMutationService(
            settings=settings,
            facts=facts,
            processor=processor,
            ledger=ledger,
        ),
        facts,
        ledger,
        DreamRepository(database),
    )


async def _fact_with_evidence(
    facts: MemoryFactService,
    ledger: EventLedgerRepository,
    *,
    message_id: str,
    memory_key: str,
    content: str,
    source_type: MemorySourceType = MemorySourceType.AUTOMATIC,
    authority: MemoryAuthority = MemoryAuthority.SELF_REPORT,
    kind: MemoryKind = MemoryKind.FACT,
) -> MemoryFact:
    event, _ = await ledger.append(
        bot_user_id="8000",
        platform_message_id=message_id,
        scope_type=ScopeType.GROUP,
        sender_user_id="1001",
        direction="inbound",
        content=content,
        group_id="3001",
    )
    return await facts.remember(
        MemoryFactCreate(
            scope_type=MemoryScopeType.PERSON_GROUP,
            subject_user_id="1001",
            group_id="3001",
            kind=kind,
            memory_key=memory_key,
            category="profile",
            content=content,
            source_type=source_type,
            authority=authority,
        ),
        evidence=MemoryEvidenceCreate(
            event_id=event.id,
            source_speaker_user_id="1001",
            relation=(
                MemoryEvidenceRelation.EXPLICIT_COMMAND
                if source_type is MemorySourceType.EXPLICIT
                else MemoryEvidenceRelation.SELF_STATEMENT
            ),
            confidence=1.0,
            authority=authority,
            excerpt=content,
        ),
    )


def _empty_dream_statistics() -> DreamPlanStatistics:
    return DreamPlanStatistics(
        eligible_facts=0,
        ready_facts=0,
        missing_embeddings=0,
        ambiguous_bot_facts=0,
        partitions=0,
        candidate_clusters=0,
        isolated_facts=0,
        estimated_model_calls=0,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation",
    [DreamOperationType.SYNTHESIZE, DreamOperationType.KEEP, DreamOperationType.CONTEST],
)
async def test_single_source_dream_uses_saved_model_output_and_original_receipt(
    database, operation
):
    mutations, facts, ledger, dreams = _services(database)
    await PeopleRepository(database).observe(user_id="8000", nickname="Yuki", is_bot=True)
    source = await _fact_with_evidence(
        facts,
        ledger,
        message_id="single-source-synthesis",
        memory_key="drink:coffee",
        content="我喜欢喝不加糖的美式咖啡",
    )
    evidence = (await facts.list_evidence(source.id, limit=10))[0]
    provider = FakeLLMProvider(
        responder=lambda _: json.dumps(
            {
                "actions": [
                    {
                        "operation": operation.value,
                        "source_refs": ["memory_1"],
                        "anchor_ref": "memory_1",
                        "content": "喜欢无糖美式咖啡",
                        "importance": 4,
                    }
                ]
            },
            ensure_ascii=False,
        )
    )
    service = object.__new__(DreamService)
    service._settings = make_settings(database.url)
    service._facts, service._mutations, service._repository = facts, mutations, dreams
    service._structured = StructuredTaskRunner(InjectedModelExecutor(provider))
    service._concurrency = ConcurrencyManager(2)
    run = await dreams.create_run(
        mode=DreamRunMode.FULL,
        statistics=_empty_dream_statistics(),
        clusters=(
            (
                "single",
                "partition",
                "8000",
                "fact",
                (source.id,),
                service._cluster_fingerprint((source,)),
            ),
        ),
        snapshot_max_fact_id=source.id,
        actor_user_id=None,
        scheduled_slot=None,
    )
    assert await dreams.start_run(run.public_id)
    cluster = await dreams.claim_next_cluster(run.public_id)
    preview = await service.preview_cluster(run.public_id, cluster.id)
    assert preview.actions[0].source_refs == ("memory_1",)
    assert await service.process_cluster(run, cluster) == (0, 1, True)
    assert len(provider.requests) == 1
    async with database.sessions() as session:
        stored_operation = await session.scalar(select(MemoryDreamOperationModel))
        result = await session.scalar(select(MemoryDreamOperationResultModel))
        receipt = await session.scalar(select(MemoryMutationReceiptModel))
        identities = stored_operation.id, result.fact_id, receipt.id
    replacement = await facts.get_fact(result.fact_id)
    if operation is DreamOperationType.SYNTHESIZE:
        assert replacement.content == "喜欢无糖美式咖啡"
        assert replacement.supersedes_id == source.id and replacement.status is MemoryStatus.ACTIVE
        assert (await facts.get_fact(source.id)).status is MemoryStatus.SUPERSEDED
    else:
        assert replacement.id == source.id and replacement.content == source.content
        assert replacement.status is MemoryStatus.ACTIVE
        assert replacement.conflict_state is (
            MemoryConflictState.CLEAR
            if operation is DreamOperationType.KEEP
            else MemoryConflictState.CONTESTED
        )
    copied = (await facts.list_evidence(replacement.id, limit=10))[0]
    assert (copied.event_id, copied.excerpt, copied.authority, copied.confidence) == (
        evidence.event_id,
        evidence.excerpt,
        evidence.authority,
        evidence.confidence,
    )
    assert await dreams.reset_processing_after_restart() == 1
    assert await dreams.reset_processing_after_restart() == 0
    assert await dreams.claim_next_cluster(run.public_id) is None
    assert len(provider.requests) == 1
    async with database.sessions() as session:
        stored_operation = await session.scalar(select(MemoryDreamOperationModel))
        result = await session.scalar(select(MemoryDreamOperationResultModel))
        receipt = await session.scalar(select(MemoryMutationReceiptModel))
        assert (stored_operation.id, result.fact_id, receipt.id) == identities
        assert await session.scalar(select(func.count(MemoryMutationReceiptModel.id))) == 1
        assert await session.scalar(select(func.count(MemoryEvidenceModel.id))) == (
            2 if operation is DreamOperationType.SYNTHESIZE else 1
        )


@pytest.mark.asyncio
async def test_dream_merge_is_atomic_audited_and_reversible(database: Database) -> None:
    mutations, facts, ledger, dreams = _services(database)
    await PeopleRepository(database).observe(user_id="8000", nickname="Yuki", is_bot=True)
    first = await _fact_with_evidence(
        facts,
        ledger,
        message_id="dream-first",
        memory_key="drink:coffee",
        content="我平时喜欢喝美式咖啡",
    )
    second = await _fact_with_evidence(
        facts,
        ledger,
        message_id="dream-second",
        memory_key="preference:americano",
        content="我喜欢不加糖的美式",
    )
    anchor = mutations.select_dream_anchor((first, second))
    run = await dreams.create_run(
        mode=DreamRunMode.FULL,
        statistics=DreamPlanStatistics(
            eligible_facts=2,
            ready_facts=2,
            missing_embeddings=0,
            ambiguous_bot_facts=0,
            partitions=1,
            candidate_clusters=1,
            isolated_facts=0,
            estimated_model_calls=1,
        ),
        clusters=(("cluster", "partition", "8000", "fact", (first.id, second.id), "fp"),),
        snapshot_max_fact_id=max(first.id, second.id),
        actor_user_id="1001",
        scheduled_slot=None,
    )
    assert await dreams.start_run(run.public_id)
    cluster = await dreams.claim_next_cluster(run.public_id)
    assert cluster is not None

    async with facts.repository.transaction(read_snapshot=True) as session:
        source_rows: list[MemoryFact] = []
        for fact_id in (first.id, second.id):
            fact = await facts.repository.get_fact(fact_id, session=session)
            assert fact is not None
            source_rows.append(fact)
        sources = tuple(source_rows)
        await mutations.prepare_dream_evidence(sources, anchor_fact_id=anchor.id, session=session)
        operation = await dreams.create_operation(
            cluster_id=cluster.id,
            action_index=1,
            operation_type=DreamOperationType.MERGE,
            source_facts=sources,
            anchor_fact_id=anchor.id,
            session=session,
        )
        result = await mutations.mutate_dream(
            dream_operation_id=operation.id,
            operation_type=DreamOperationType.MERGE,
            source_facts=sources,
            anchor_fact_id=anchor.id,
            content=None,
            importance=None,
            bot_user_id="8000",
            run_public_id=run.public_id,
            session=session,
        )
        current_sources: dict[int, MemoryFact] = {}
        for fact_id in (first.id, second.id):
            current = await facts.repository.get_fact(fact_id, session=session)
            assert current is not None
            current_sources[fact_id] = current
        output = current_sources[anchor.id]
        await dreams.commit_operation(
            operation.id,
            output_fact_id=result.output_fact_id,
            output_results=((output.id, fact_signature(output)),),
            added_evidence_ids=result.added_evidence_ids,
            added_relation_ids=result.added_relation_ids,
            result_signature=fact_signature(output),
            source_signatures={
                fact_id: fact_signature(current) for fact_id, current in current_sources.items()
            },
            session=session,
        )

    assert await dreams.reset_processing_after_restart() == 1
    recovered_run = await dreams.get_run(run.public_id)
    recovered_page = await dreams.run_page(run.public_id)
    assert recovered_run is not None and recovered_run.completed_clusters == 1
    assert recovered_page.clusters[0].status.value == "completed"
    assert recovered_page.clusters[0].operation_count == 1

    merged_source_id = first.id if anchor.id == second.id else second.id
    merged_source = await facts.get_fact(merged_source_id)
    merged_anchor = await facts.get_fact(anchor.id)
    assert merged_source is not None and merged_source.status is MemoryStatus.SUPERSEDED
    assert merged_anchor is not None and merged_anchor.status is MemoryStatus.ACTIVE
    assert len(await facts.list_evidence(anchor.id, limit=10)) == 2
    async with database.sessions() as session:
        receipt = await session.scalar(
            select(MemoryMutationReceiptModel).where(
                MemoryMutationReceiptModel.dream_operation_id == operation.id,
                MemoryMutationReceiptModel.reason_code == "memory_dream_merge",
            )
        )
        assert receipt is not None
        assert receipt.trigger_event_id is None
        assert receipt.trigger_source_type == "dream_operation"

    unrelated = await _fact_with_evidence(
        facts,
        ledger,
        message_id="outside-dream",
        memory_key="outside:topic",
        content="我喜欢散步",
    )
    unrelated_evidence = (await facts.list_evidence(unrelated.id, limit=10))[0]
    async with facts.repository.transaction() as session:
        assert await facts.repository.add_relation(
            source_fact_id=unrelated.id,
            target_fact_id=first.id,
            relation_type=MemoryFactRelationType.SUPPORTS,
            confidence=1.0,
            source_event_id=None,
            session=session,
        )
        unrelated_relation = await session.scalar(
            select(MemoryFactRelationModel).where(
                MemoryFactRelationModel.source_fact_id == unrelated.id,
                MemoryFactRelationModel.target_fact_id == first.id,
            )
        )
        assert unrelated_relation is not None
    # A reused ID must not authorize deletion outside this operation. Each
    # rejected attempt rolls back its tampered fixture and creates no receipt.
    for field, stale_id, error in (
        ("added_evidence_ids_json", unrelated_evidence.id, "Dream evidence reference"),
        ("added_relation_ids_json", unrelated_relation.id, "Dream relation reference"),
    ):
        with pytest.raises(RuntimeError, match=error):
            async with facts.repository.transaction(read_snapshot=True) as session:
                await facts.prepare_evidence_write((first.id, second.id), session=session)
                stored = await session.get(MemoryDreamOperationModel, operation.id)
                assert stored is not None
                setattr(stored, field, json.dumps([stale_id]))
                await session.flush()
                await mutations.rollback_dream_operation(
                    public_id=operation.public_id,
                    session=session,
                )
        assert (await facts.list_evidence(unrelated.id, limit=10))[0].id == unrelated_evidence.id
        async with database.sessions() as session:
            assert await session.get(MemoryFactRelationModel, unrelated_relation.id) is not None

    async with facts.repository.transaction(read_snapshot=True) as session:
        affected = await mutations.rollback_dream_operation(
            public_id=operation.public_id,
            session=session,
        )
    assert set(affected) == {first.id, second.id}
    restored_source = await facts.get_fact(merged_source_id)
    restored_anchor = await facts.get_fact(anchor.id)
    assert restored_source is not None and restored_source.status is MemoryStatus.ACTIVE
    assert restored_anchor is not None and restored_anchor.status is MemoryStatus.ACTIVE
    assert len(await facts.list_evidence(anchor.id, limit=10)) == 1
    async with database.sessions() as session:
        rolled_back = await session.get(MemoryDreamOperationModel, operation.id)
        rollback_receipts = int(
            await session.scalar(
                select(func.count())
                .select_from(MemoryMutationReceiptModel)
                .where(
                    MemoryMutationReceiptModel.dream_operation_id == operation.id,
                    MemoryMutationReceiptModel.reason_code == "memory_dream_rollback",
                )
            )
            or 0
        )
        evidence_count = int(
            await session.scalar(
                select(func.count())
                .select_from(MemoryEvidenceModel)
                .where(MemoryEvidenceModel.fact_id.in_((first.id, second.id)))
            )
            or 0
        )
    assert rolled_back is not None
    assert rolled_back.status == DreamOperationStatus.ROLLED_BACK.value
    assert rollback_receipts == 1
    assert evidence_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "output_count", "evidence_count"),
    [(MemoryKind.FACT, 5, 13), (MemoryKind.PREFERENCE, 2, 1), (MemoryKind.EPISODE, 2, 1)],
)
async def test_recompose_is_atomic_partial_one_to_many_and_reversible(
    database: Database,
    kind: MemoryKind,
    output_count: int,
    evidence_count: int,
) -> None:
    mutations, facts, ledger, dreams = _services(database)
    await PeopleRepository(database).observe(user_id="8000", nickname="Yuki", is_bot=True)
    source_content = "上午处理点单失败；晚上和群友讨论音乐。" * 60
    first_output = "一次点单工具链失败后，我确认需要减少无意义的中间步骤。" * 12
    second_output = "晚上和群友集中聊了音乐，留下了几次有趣的推荐和争论。" * 12
    source = await _fact_with_evidence(
        facts,
        ledger,
        message_id="dream-recompose-source",
        memory_key="episode:mixed-day",
        content=source_content,
        kind=kind,
    )
    mutations._settings = mutations._settings.model_copy(
        update={"memory_dream_evidence_per_fact": 1}
    )
    for index in range(1, evidence_count):
        event, _ = await ledger.append(
            bot_user_id="8000",
            platform_message_id=f"dream-recompose-evidence-{index}",
            scope_type=ScopeType.GROUP,
            sender_user_id="1001",
            direction="inbound",
            content=source_content,
            group_id="3001",
        )
        await facts.confirm_fact(
            source.id,
            MemoryEvidenceCreate(
                event_id=event.id,
                source_speaker_user_id="1001",
                relation=MemoryEvidenceRelation.CONFIRMATION,
                authority=MemoryAuthority.SELF_REPORT,
                excerpt=source_content,
            ),
        )
    untouched = await _fact_with_evidence(
        facts,
        ledger,
        message_id="dream-recompose-unhandled",
        memory_key="unhandled:source",
        content="尚未整理的独立资料",
        kind=kind,
    )
    run = await dreams.create_run(
        mode=DreamRunMode.FULL,
        statistics=DreamPlanStatistics(
            eligible_facts=2,
            ready_facts=2,
            missing_embeddings=0,
            ambiguous_bot_facts=0,
            partitions=1,
            candidate_clusters=1,
            isolated_facts=0,
            estimated_model_calls=1,
        ),
        clusters=(("recompose", "partition", "8000", kind.value, (source.id, untouched.id), "fp"),),
        snapshot_max_fact_id=untouched.id,
        actor_user_id="1001",
        scheduled_slot=None,
    )
    assert await dreams.start_run(run.public_id)
    cluster = await dreams.claim_next_cluster(run.public_id)
    assert cluster is not None

    async with facts.repository.transaction(read_snapshot=True) as session:
        current_source = await facts.repository.get_fact(source.id, session=session)
        assert current_source is not None
        action = DreamAction(
            operation=DreamOperationType.RECOMPOSE,
            source_refs=("memory_1", "memory_2"),
            anchor_ref="memory_1",
            content="这条上层说明不作为 recompose 正文",
            importance=5,
            outputs=tuple(
                DreamRecomposeOutput(
                    focus="长期资料",
                    source_refs=("memory_1",),
                    content=first_output if index % 2 == 0 else second_output,
                    importance=3,
                )
                for index in range(output_count)
            ),
        )
        recompose_outputs = tuple(
            DreamRecomposePlan(
                source_facts=(current_source,), content=item.content, importance=item.importance
            )
            for item in action.outputs
        )
        await mutations.prepare_dream_evidence(
            (current_source, untouched),
            anchor_fact_id=current_source.id,
            recompose_outputs=recompose_outputs,
            session=session,
        )
        operation = await dreams.create_operation(
            cluster_id=cluster.id,
            action_index=1,
            operation_type=DreamOperationType.RECOMPOSE,
            source_facts=(current_source, untouched),
            anchor_fact_id=current_source.id,
            session=session,
        )
        result = await mutations.mutate_dream(
            dream_operation_id=operation.id,
            operation_type=DreamOperationType.RECOMPOSE,
            source_facts=(current_source, untouched),
            anchor_fact_id=current_source.id,
            content=action.content,
            importance=action.importance,
            recompose_outputs=recompose_outputs,
            bot_user_id="8000",
            run_public_id=run.public_id,
            session=session,
        )
        assert len(result.output_fact_ids) == output_count
        loaded_outputs: list[MemoryFact] = []
        for fact_id in result.output_fact_ids:
            item = await facts.repository.get_fact(fact_id, session=session)
            if item is not None:
                loaded_outputs.append(item)
        outputs = tuple(loaded_outputs)
        assert len(outputs) == output_count
        assert all(item.kind is kind and item.memory_key == source.memory_key for item in outputs)
        latest_source = await facts.repository.get_fact(source.id, session=session)
        assert latest_source is not None
        await dreams.commit_operation(
            operation.id,
            output_fact_id=result.output_fact_id,
            output_results=tuple((item.id, fact_signature(item)) for item in outputs),
            added_evidence_ids=result.added_evidence_ids,
            added_relation_ids=result.added_relation_ids,
            result_signature=fact_signature(outputs[0]),
            source_signatures={
                source.id: fact_signature(latest_source),
                untouched.id: fact_signature(untouched),
            },
            session=session,
        )

    superseded = await facts.get_fact(source.id)
    assert superseded is not None and superseded.status is MemoryStatus.SUPERSEDED
    assert await facts.get_fact(untouched.id) == untouched
    for fact_id in result.output_fact_ids:
        fact = await facts.get_fact(fact_id)
        assert fact is not None and fact.status is MemoryStatus.ACTIVE
        assert len(await facts.list_evidence(fact_id)) == evidence_count
    async with database.sessions() as session:
        persisted_results = tuple(
            (
                await session.scalars(
                    select(MemoryDreamOperationResultModel).where(
                        MemoryDreamOperationResultModel.operation_id == operation.id
                    )
                )
            ).all()
        )
    assert {item.fact_id for item in persisted_results} == set(result.output_fact_ids)

    async with facts.repository.transaction(read_snapshot=True) as session:
        affected = await mutations.rollback_dream_operation(
            public_id=operation.public_id,
            session=session,
        )
    assert set(affected) == {source.id, untouched.id, *result.output_fact_ids}
    restored = await facts.get_fact(source.id)
    assert restored is not None and restored.status is MemoryStatus.ACTIVE
    assert await facts.get_fact(untouched.id) == untouched
    for fact_id in result.output_fact_ids:
        invalidated = await facts.get_fact(fact_id)
        assert invalidated is not None and invalidated.status is MemoryStatus.INVALIDATED


@pytest.mark.asyncio
async def test_dream_never_modifies_an_explicit_anchor(database: Database) -> None:
    mutations, facts, ledger, _dreams = _services(database)
    explicit = await _fact_with_evidence(
        facts,
        ledger,
        message_id="dream-explicit",
        memory_key="identity:explicit",
        content="这是用户明确要求长期保留的事实",
        source_type=MemorySourceType.EXPLICIT,
        authority=MemoryAuthority.EXPLICIT,
    )
    automatic = await _fact_with_evidence(
        facts,
        ledger,
        message_id="dream-automatic",
        memory_key="identity:auto",
        content="这是自动提取的近似事实",
    )
    async with facts.repository.transaction(read_snapshot=True) as session:
        with pytest.raises(ValueError, match="explicit memory anchor"):
            await mutations.mutate_dream(
                dream_operation_id=1,
                operation_type=DreamOperationType.SYNTHESIZE,
                source_facts=(explicit, automatic),
                anchor_fact_id=explicit.id,
                content="模型不得改写这个显式事实",
                importance=5,
                bot_user_id="8000",
                run_public_id="protected",
                session=session,
            )


@pytest.mark.asyncio
async def test_dream_resolution_records_conflict_provenance(database: Database) -> None:
    mutations, facts, ledger, dreams = _services(database)
    await PeopleRepository(database).observe(user_id="8000", nickname="Yuki", is_bot=True)
    preferred = await _fact_with_evidence(
        facts,
        ledger,
        message_id="dream-resolve-preferred",
        memory_key="location:current",
        content="我现在住在连江",
    )
    rejected = await _fact_with_evidence(
        facts,
        ledger,
        message_id="dream-resolve-rejected",
        memory_key="location:old",
        content="我现在住在福州",
    )
    run = await dreams.create_run(
        mode=DreamRunMode.FULL,
        statistics=DreamPlanStatistics(
            eligible_facts=2,
            ready_facts=2,
            missing_embeddings=0,
            ambiguous_bot_facts=0,
            partitions=1,
            candidate_clusters=1,
            isolated_facts=0,
            estimated_model_calls=1,
        ),
        clusters=(("resolve", "partition", "8000", "fact", (preferred.id, rejected.id), "fp"),),
        snapshot_max_fact_id=rejected.id,
        actor_user_id="1001",
        scheduled_slot=None,
    )
    assert await dreams.start_run(run.public_id)
    cluster = await dreams.claim_next_cluster(run.public_id)
    assert cluster is not None
    async with facts.repository.transaction(read_snapshot=True) as session:
        await mutations.prepare_dream_evidence(
            (preferred, rejected), anchor_fact_id=preferred.id, session=session
        )
        operation = await dreams.create_operation(
            cluster_id=cluster.id,
            action_index=1,
            operation_type=DreamOperationType.RESOLVE,
            source_facts=(preferred, rejected),
            anchor_fact_id=preferred.id,
            session=session,
        )
        result = await mutations.mutate_dream(
            dream_operation_id=operation.id,
            operation_type=DreamOperationType.RESOLVE,
            source_facts=(preferred, rejected),
            anchor_fact_id=preferred.id,
            content=None,
            importance=None,
            bot_user_id="8000",
            run_public_id=run.public_id,
            session=session,
        )
        current_preferred = await facts.repository.get_fact(preferred.id, session=session)
        current_rejected = await facts.repository.get_fact(rejected.id, session=session)
        assert current_preferred is not None and current_rejected is not None
        await dreams.commit_operation(
            operation.id,
            output_fact_id=result.output_fact_id,
            output_results=((current_preferred.id, fact_signature(current_preferred)),),
            added_evidence_ids=result.added_evidence_ids,
            added_relation_ids=result.added_relation_ids,
            result_signature=fact_signature(current_preferred),
            source_signatures={
                preferred.id: fact_signature(current_preferred),
                rejected.id: fact_signature(current_rejected),
            },
            session=session,
        )

    resolved = await facts.get_fact(rejected.id)
    assert resolved is not None and resolved.status is MemoryStatus.INVALIDATED
    async with database.sessions() as session:
        relation = await session.scalar(
            select(MemoryFactRelationModel).where(
                MemoryFactRelationModel.source_fact_id == rejected.id,
                MemoryFactRelationModel.target_fact_id == preferred.id,
                MemoryFactRelationModel.relation_type == MemoryFactRelationType.CONTRADICTS.value,
            )
        )
    assert relation is not None


@pytest.mark.asyncio
async def test_dream_recompose_consumes_actual_outputs_and_normalizes_model_metadata(database):
    mutations, facts, ledger, dreams = _services(database)
    await PeopleRepository(database).observe(user_id="8000", nickname="Yuki", is_bot=True)
    sources = tuple(
        [
            await _fact_with_evidence(
                facts,
                ledger,
                message_id=f"actual-source-{index}",
                memory_key=f"source:{index}",
                content=f"实际来源内容 {index}",
            )
            for index in range(3)
        ]
    )
    provider = FakeLLMProvider(
        responder=lambda _: json.dumps(
            {
                "planner_note": "无消费元数据",
                "actions": [
                    {
                        "operation": "recompose",
                        "source_refs": ["memory_1", "memory_2", "memory_3", "memory_1"],
                        "notes": "仅实际产物消费 memory_1",
                        "outputs": [
                            {
                                "focus": "第一个来源",
                                "source_refs": ["memory_1", "memory_1"],
                                "content": "重组第一个来源",
                                "importance": 4,
                                "metadata": {"unused": True},
                            }
                        ],
                    },
                    {
                        "operation": "synthesize",
                        "source_refs": ["memory_2", "memory_2"],
                        "anchor_ref": "memory_2",
                        "content": "继续整理第二个来源",
                        "importance": 4,
                        "metadata": {"unused": True},
                    },
                ],
            },
            ensure_ascii=False,
        )
    )
    service = object.__new__(DreamService)
    service._settings = make_settings(database.url)
    service._facts, service._mutations, service._repository = facts, mutations, dreams
    service._structured = StructuredTaskRunner(InjectedModelExecutor(provider))
    service._concurrency = ConcurrencyManager(2)
    run = await dreams.create_run(
        mode=DreamRunMode.FULL,
        statistics=_empty_dream_statistics(),
        clusters=(
            (
                "actual",
                "partition",
                "8000",
                "fact",
                tuple(row.id for row in sources),
                service._cluster_fingerprint(sources),
            ),
        ),
        snapshot_max_fact_id=sources[-1].id,
        actor_user_id=None,
        scheduled_slot=None,
    )
    assert await dreams.start_run(run.public_id)
    cluster = await dreams.claim_next_cluster(run.public_id)
    preview = await service.preview_cluster(run.public_id, cluster.id)
    assert preview.actions[0].source_refs == ("memory_1", "memory_2", "memory_3")
    assert preview.actions[0].outputs[0].source_refs == ("memory_1",)
    assert preview.actions[1].source_refs == ("memory_2",)
    assert await service.process_cluster(run, cluster) == (0, 2, True)
    assert len(provider.requests) == 1
    page = await dreams.run_page(run.public_id)
    assert [operation.source_fact_ids for operation in page.operations] == [
        (sources[0].id,),
        (sources[1].id,),
    ]
    for operation, source in zip(page.operations, sources, strict=False):
        output = await facts.get_fact(operation.output_fact_id)
        evidence = await facts.list_evidence(output.id)
        original = await facts.list_evidence(source.id)
        assert len(evidence) == 1 and evidence[0].event_id == original[0].event_id
        assert (await facts.get_fact(source.id)).status is MemoryStatus.SUPERSEDED
    assert (await facts.get_fact(sources[2].id)).status is MemoryStatus.ACTIVE
    assert (await facts.get_fact(sources[2].id)).content == sources[2].content
    assert await dreams.reset_processing_after_restart() == 1
    assert await dreams.claim_next_cluster(run.public_id) is None
    assert len(provider.requests) == 1
    async with database.sessions() as session:
        assert await session.scalar(select(func.count(MemoryMutationReceiptModel.id))) == 2
