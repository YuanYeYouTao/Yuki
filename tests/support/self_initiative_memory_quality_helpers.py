"""Silent SELF evidence must survive the same quality and lineage read paths."""

from tests.unit.test_self_initiative_memory import claim, finish, record, seed

from qq_ai_bot.memory.enums import MemoryKind, MemoryScopeType, SelfMemoryVisibility
from qq_ai_bot.memory.mutation.models import (
    MemoryDecisionActorType,
    MemoryMutationContext,
    MemoryMutationOperation,
    MemoryMutationRequest,
    MemoryMutationTarget,
)
from qq_ai_bot.memory.subjects import ResolvedSubject


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
