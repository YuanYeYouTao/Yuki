"""Permanent independent evidence survives normal cleanup and historical run reads."""

import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import select, update
from tests.conftest import make_settings
from tests.support.model_executor import InjectedModelExecutor
from tests.unit.test_self_initiative_memory import claim, finish, record, seed

from qq_ai_bot.llm.fake import FakeLLMProvider
from qq_ai_bot.memory.dream.db_models import (
    MemoryEvidenceCompactionItemModel,
    MemoryEvidenceCompactionRunModel,
)
from qq_ai_bot.memory.dream.repository import DreamRepository
from qq_ai_bot.memory.enums import MemoryScopeType, SelfMemoryVisibility
from qq_ai_bot.memory.models import MemoryFactQuery
from qq_ai_bot.memory.self_reflection.repository import SelfReflectionRepository
from qq_ai_bot.memory.self_reflection.service import SelfReflectionService
from qq_ai_bot.persistence.models import MemoryToolReceiptModel


async def test_nine_original_tool_sources_survive_completed_reflection_and_receipt_cleanup(
    database,
):
    mutations, facts, source, run_id = await seed(database)
    for index in range(9):
        await record(
            database,
            source,
            run_id,
            f"independent-tool-{index}",
            result_excerpt=f"已完成独立步骤 {index}",
        )
    await finish(database, run_id)
    (batch,) = await claim(database)
    repository = SelfReflectionRepository(database)
    receipts = await repository.tool_receipts(batch)
    original_ids = tuple(receipt.id for receipt in receipts)
    assert len(original_ids) == 9
    provider = FakeLLMProvider(
        lambda _: json.dumps(
            {
                "episodes": [
                    {
                        "importance": 4,
                        "passages": [
                            {
                                "content": "我完成了九个独立步骤",
                                "evidence_refs": [f"tool_{i}" for i in range(1, 10)],
                            }
                        ],
                    }
                ]
            }
        )
    )
    service = SelfReflectionService(
        settings=make_settings(database.url, self_memory_enabled=True),
        repository=repository,
        mutations=mutations,
        models=InjectedModelExecutor(provider),
    )
    assert await service.reflect(batch) == (1, 1)
    await repository.complete(batch, proposals=1, committed=1)
    stored = await facts.repository.list_facts(
        MemoryFactQuery(
            scope_type=MemoryScopeType.SELF,
            visibility_type=SelfMemoryVisibility.GROUP,
            canonical_visibility_space_id=batch.state.canonical_space_id,
        )
    )
    assert len(stored) == 1
    evidence = await facts.list_evidence(stored[0].id)
    assert {item.tool_receipt_id for item in evidence} == set(original_ids)
    now = datetime.now(UTC)
    async with database.immediate_session() as writer:
        await writer.execute(
            update(MemoryToolReceiptModel).values(
                expires_at=now - timedelta(days=1),
            )
        )
        historical = MemoryEvidenceCompactionRunModel(
            public_id=str(uuid4()),
            status="partial_failed",
            scan_after_fact_id=stored[0].id,
            scanned_facts=1,
            completed_items=0,
            skipped_items=0,
            failed_items=1,
            evidence_before=9,
            evidence_after=9,
            error_category="old-interrupted-run",
            created_at=now,
            updated_at=now,
            completed_at=now,
        )
        writer.add(historical)
        await writer.flush()
        writer.add(
            MemoryEvidenceCompactionItemModel(
                run_id=historical.id,
                fact_id=stored[0].id,
                provenance_type="self_reflection",
                status="failed",
                evidence_before=9,
                evidence_after=9,
                deleted_count=0,
                error_category="old-interrupted-item",
                created_at=now,
                updated_at=now,
            )
        )
    assert await repository.cleanup_receipts() == 0
    assert [item.id for item in await facts.list_evidence(stored[0].id)] == [
        item.id for item in evidence
    ]
    health = await DreamRepository(database).health(enabled=True)
    assert health.compaction_failed == 1 and health.compaction_evidence_after == 9
    async with database.sessions() as reader:
        assert set(await reader.scalars(select(MemoryToolReceiptModel.id))) == set(original_ids)
        item = (await reader.scalars(select(MemoryEvidenceCompactionItemModel))).one()
        assert item.error_category == "old-interrupted-item" and item.deleted_count == 0
