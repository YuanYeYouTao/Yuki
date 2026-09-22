"""Silent SELF evidence must survive the same quality and lineage read paths."""

import pytest
from sqlalchemy import select, text
from tests.unit.test_self_initiative_memory import claim, finish, record, seed

from qq_ai_bot.memory.dream.repository import DreamRepository
from qq_ai_bot.memory.enums import MemoryKind, MemoryScopeType, SelfMemoryVisibility
from qq_ai_bot.memory.lineage import MemoryLineageService
from qq_ai_bot.memory.models import MemoryEvidenceCreate
from qq_ai_bot.memory.mutation.models import (
    MemoryDecisionActorType,
    MemoryMutationContext,
    MemoryMutationOperation,
    MemoryMutationRequest,
    MemoryMutationTarget,
)
from qq_ai_bot.memory.quality.audit import MemoryProductionQualityAudit
from qq_ai_bot.memory.quality.hygiene import MemoryProvenanceHygiene
from qq_ai_bot.memory.repository import MemoryFactRepository
from qq_ai_bot.memory.self_origin import (
    read_self_seed_candidates,
    receipt_evidence_readable,
    sql_self_receipt_evidence_predicate,
)
from qq_ai_bot.memory.subjects import ResolvedSubject
from qq_ai_bot.persistence.models import (
    MemoryEvidenceModel,
    MemoryFactModel,
    MemoryToolReceiptModel,
)


async def reflection_fact(database):
    service, facts, event, run_id = await seed(database)
    await record(database, event, run_id)
    await record(database, event, run_id, "call-2")
    await finish(database, run_id)
    (batch,) = await claim(database)
    result = await service.mutate_resolved(
        MemoryMutationRequest(
            operation=MemoryMutationOperation.CREATE,
            target=MemoryMutationTarget(subject_ref="self", scope_type=MemoryScopeType.SELF),
            new_content="我独立完成了曲线绘图并校验了结果。",
            memory_key="self_episode:quality",
            category="self_episode",
            kind=MemoryKind.EPISODE,
            importance=4,
            reason="实际独立经历",
            evidence_quote="已生成并校验曲线绘图",
        ),
        MemoryMutationContext(
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
        ),
        target=ResolvedSubject(
            MemoryScopeType.SELF, None, None, SelfMemoryVisibility.GROUP, None, "3001"
        ),
        self_reflection_result=(batch.run_id, "episode", 0),
    )
    assert result.ok, result.reason_code
    return facts, event, run_id, batch, result.new_fact_id


@pytest.mark.asyncio
async def test_silent_self_evidence_survives_audit_hygiene_lineage_and_dream_identity(database):
    facts, event, run_id, batch, fact_id = await reflection_fact(database)
    async with database.sessions() as session, session.begin():
        receipt = await session.get(MemoryToolReceiptModel, batch.last_receipt_id)
        assert await MemoryFactRepository(database).add_evidence(
            fact_id,
            MemoryEvidenceCreate(
                tool_receipt_id=receipt.id,
                source_speaker_user_id=receipt.bot_user_id,
                relation="agent_reflection",
                authority="agent_reflection",
                excerpt="已生成并校验曲线绘图",
            ),
            session=session,
        )
    fact = await facts.get_fact(fact_id)
    assert fact.evidence_count == 2
    audit = await MemoryProductionQualityAudit(database).run()
    assert (
        next(i for i in audit.issues if i.issue_code == "evidence_tool_source_invalid").count == 0
    )
    assert next(i for i in audit.issues if i.issue_code == "evidence_duplicate_event").count == 0
    assert fact_id not in (await MemoryProvenanceHygiene(database).scan()).invalid_fact_ids
    page = await MemoryLineageService(database).list_evidence_lineage(fact_id)
    assert page.items[0].event_id is None and page.items[0].initiative_run_id == run_id
    windows = [item for item in page.items if item.kind == "reflection_initiative_window"]
    assert {item.tool_receipt_id for item in windows} == {
        batch.first_receipt_id,
        batch.last_receipt_id,
    }
    assert all(item.initiative_run_id == run_id and item.event_id is None for item in windows)
    async with database.sessions() as session:
        assert await DreamRepository._fact_bot_ids(fact, session=session) == {"self"}
    candidates = await read_self_seed_candidates(
        database, canonical_conversation_id=event.canonical_conversation_id
    )
    assert [item.id for item in candidates] == [fact_id]
    assert await read_self_seed_candidates(database, canonical_conversation_id="missing") == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("corruption", ["excerpt", "speaker", "private_visibility"])
async def test_self_receipt_sql_and_python_reject_invalid_provenance_identically(
    database, corruption
):
    facts, event, _, _, fact_id = await reflection_fact(database)
    async with database.sessions() as session, session.begin():
        fact = await session.get(MemoryFactModel, fact_id)
        evidence = await session.scalar(
            select(MemoryEvidenceModel).where(MemoryEvidenceModel.fact_id == fact_id)
        )
        receipt = await session.get(MemoryToolReceiptModel, evidence.tool_receipt_id)
        assert await receipt_evidence_readable(
            session, fact=fact, evidence=evidence, receipt=receipt
        )
        if corruption == "excerpt":
            evidence.excerpt = "不在真实工具回执中的内容"
        elif corruption == "speaker":
            evidence.source_speaker_user_id = "1001"
        else:
            fact.visibility_type = "private"
            fact.canonical_visibility_space_id = None
            fact.canonical_visibility_person_id = event.author_person_id
        await session.flush()
        assert not await receipt_evidence_readable(
            session, fact=fact, evidence=evidence, receipt=receipt
        )
        count = await session.scalar(
            text(
                "SELECT count(*) FROM memory_facts f JOIN memory_evidence e ON e.fact_id=f.id "
                "JOIN memory_tool_receipts t ON t.id=e.tool_receipt_id "
                f"WHERE f.id=:id AND {sql_self_receipt_evidence_predicate()}"
            ),
            {"id": fact_id},
        )
        assert count == 0
    assert await facts.list_evidence(fact_id) == ()
    assert (
        await read_self_seed_candidates(
            database, canonical_conversation_id=event.canonical_conversation_id
        )
        == ()
    )
    assert fact_id in (await MemoryProvenanceHygiene(database).scan()).invalid_fact_ids
