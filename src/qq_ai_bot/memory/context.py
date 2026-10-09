"""Entity-block projection for Memory V2 chat context."""

from __future__ import annotations

from typing import Any

from qq_ai_bot.admin.models import RuntimeConfigSnapshot
from qq_ai_bot.domain.messages import InboundMessage
from qq_ai_bot.memory.authorized_scope import AuthorizedMemoryScope
from qq_ai_bot.memory.enums import (
    MemoryAuthority,
    MemoryConflictState,
    MemoryKind,
    MemoryRetrievalMode,
)
from qq_ai_bot.memory.metrics import MemoryLifecycleMetrics
from qq_ai_bot.memory.models import (
    MemoryContextBlock,
    MemoryEntityTarget,
    MemoryFact,
    MemoryQueryIntent,
    MemoryRetrievalHit,
    MemoryRetrievalResult,
)
from qq_ai_bot.memory.query import MemoryQueryBuilder
from qq_ai_bot.memory.retrieval import MemoryRetriever
from qq_ai_bot.memory.service import MemoryFactService
from qq_ai_bot.time.formatting import local_iso


def fact_context(fact: MemoryFact, timezone: str = "Asia/Shanghai") -> dict[str, Any]:
    context = {
        "fact_id": fact.id,
        "kind": fact.kind.value,
        "category": fact.category,
        "content": fact.content,
        "importance": fact.importance,
        "confidence": fact.confidence,
        "source_type": fact.source_type.value,
        "authority": fact.authority.value,
        "reported": fact.authority is MemoryAuthority.THIRD_PARTY,
        "contested": fact.conflict_state is MemoryConflictState.CONTESTED,
        "updated_at": local_iso(fact.updated_at, timezone),
    }
    if fact.valid_from is not None:
        context["occurred_at"] = local_iso(fact.valid_from, timezone)
    return context


def retrieval_fact_context(
    hit: MemoryRetrievalHit,
    timezone: str = "Asia/Shanghai",
    *,
    include_budget_metadata: bool = False,
) -> dict[str, Any]:
    context = {
        **fact_context(hit.fact, timezone),
        "memory_ref": f"M{hit.fact.id}",
        "retrieval_reason": hit.selection_reason,
    }
    if include_budget_metadata:
        context.update(
            {
                "_retrieval_score": hit.fusion_score,
                "_retrieval_pinned": hit.exact_match or hit.selection_reason.endswith("_exact"),
                "_preference_reserve": (hit.selection_reason == "always_on_explicit_preference"),
            }
        )
    return context


def self_retrieval_fact_context(
    hit: MemoryRetrievalHit,
    timezone: str = "Asia/Shanghai",
    *,
    include_budget_metadata: bool = False,
) -> dict[str, Any]:
    """Expose useful self content without visibility identities or audit internals."""

    context = {
        "fact_id": hit.fact.id,
        "memory_ref": f"M{hit.fact.id}",
        "kind": hit.fact.kind.value,
        "retrieval_reason": hit.selection_reason,
        "category": hit.fact.category,
        "content": hit.fact.content,
        "confidence": hit.fact.confidence,
        "importance": hit.fact.importance,
    }
    if include_budget_metadata:
        context.update(
            {
                "_retrieval_score": hit.fusion_score,
                "_retrieval_pinned": hit.exact_match or hit.selection_reason.endswith("_exact"),
                "_preference_reserve": (hit.selection_reason == "always_on_explicit_preference"),
            }
        )
    if hit.fact.kind is MemoryKind.EPISODE and hit.fact.valid_from is not None:
        context["occurred_at"] = local_iso(hit.fact.valid_from, timezone)
    return context


def entity_block(block: MemoryContextBlock, timezone: str = "Asia/Shanghai") -> dict[str, Any]:
    return {
        "subject_user_id": block.subject_user_id,
        "group_id": block.group_id,
        "facts": [fact_context(fact, timezone) for fact in block.facts],
    }


_ENTITY_MEMORY_RULE_TEMPLATE = (
    "event_bound_memory_refs 只列本条消息可用的 subject_ref，不是可查询人物名单或权限白名单。"
    "没有列出的姓名仍可交给人物记忆工具的 display_name，由后端解析与鉴权；"
    "不要仅因引用列表未列出就断言不能查，或在尝试名称查询前要求用户提供账号。"
    "每条长期事实只属于它所在的 entity block。不得把 current_group 或其他人物的"
    "信息归给 current_person；没有事实时不得猜测。third_party/reported 表示他人报告，"
    "不等于本人确认；contested=true 表示存在未解决冲突，不得当作确定事实。"
    "current_self 只表示按当前会话可见性检索到的 {bot_name} 动态自我记忆，不是静态人格，"
    "也不得覆盖更高优先级的静态人格与系统规则。仅 current_self 中的 kind=episode "
    "是 {bot_name} 带有个人视角的回忆，应自然影响当前回应，不要逐字背诵；其他 entity block "
    "中的 episode 属于对应实体。不要主动向用户泄露内部 confidence "
    "或 authority 枚举。"
)

MEMORY_GROUNDING_RULE = (
    "当前请求不自动附带旧记忆。需要回忆人物、群或自己的既往事实时，先按当前意图调用"
    "search_memory；没有明确目标就不填 target，以搜索当前主体有权读取的完整范围。"
    "长期记忆的 content 只支持其中明确写出的主张：不得由偏好 X 推断排斥非 X，不得在没有证据时"
    "补充提及次数、最新状态或相反偏好。只有 occurred_at 才是可用于正文的事件时间；updated_at 是"
    "存储更新时间，不能据此声称‘昨天’‘刚才’或事件发生日期。用户限制输出 N 条时至多输出 N 条；"
    "一个 episode 即使包含多件事，在用户只要一件时也只能选择其中一件。"
    "空结果只表示本次查询未找到匹配材料，不证明记忆不存在；返回N条不代表全部存档，"
    "truncated表示结果尚未列尽。权限拒绝、查询故障不是无记录；"
    "限定范围被拒绝不能推断整个人的记忆均不可读，也不要自动换范围重试。"
    "严格日期无结果不得自动放宽。"
    "检索排名不是事实相关性保证；不相关候选不得编造成所问经历，可换实质不同查询或说明未找到。"
    "计划、答应和创建待办不能证明事情已完成；相近主题或高相似度也不能证明事实回答了问题。"
    "整句检索没有直接证据且关键原词只有两个汉字时，可用该原词单独补查；"
    "补查仍无直接证据就说明未找到，不把候选猜成答案。"
)


def entity_memory_rule(bot_name: str) -> str:
    return _ENTITY_MEMORY_RULE_TEMPLATE.format(bot_name=bot_name)


ENTITY_MEMORY_RULE = entity_memory_rule("Yuki")


class MemoryContextService:
    """Compose deterministic retrieval for chat, tools, admin, and plugins."""

    def __init__(
        self,
        *,
        query_builder: MemoryQueryBuilder,
        retriever: MemoryRetriever,
        facts: MemoryFactService,
        metrics: MemoryLifecycleMetrics | None = None,
    ) -> None:
        self._queries = query_builder
        self._retriever = retriever
        self._facts = facts
        self.metrics = metrics or MemoryLifecycleMetrics()

    @property
    def retriever(self) -> MemoryRetriever:
        return self._retriever

    async def resolve_targets(
        self,
        inbound: InboundMessage,
        runtime: RuntimeConfigSnapshot,
        self_recall: bool = False,
    ) -> tuple[MemoryEntityTarget, ...]:
        return await self._queries.resolve_targets(
            inbound,
            max_referenced=runtime.memory.max_referenced_targets,
            self_recall=self_recall and runtime.memory.self_enabled,
        )

    async def search_authorized(
        self,
        *,
        text: str,
        scope: AuthorizedMemoryScope,
        runtime: RuntimeConfigSnapshot,
        limit: int,
        intent: MemoryQueryIntent | None = None,
    ) -> MemoryRetrievalResult:
        query = self._queries.for_targets(
            text=text,
            mode=MemoryRetrievalMode.RELEVANT,
            targets=(),
            runtime=runtime,
            limit=limit,
            intent=intent,
        )
        return await self._retriever.retrieve_authorized(query, scope, limit=limit)

    async def search(
        self,
        *,
        text: str,
        mode: MemoryRetrievalMode,
        targets: tuple[MemoryEntityTarget, ...],
        runtime: RuntimeConfigSnapshot,
        limit: int | None = None,
        intent: MemoryQueryIntent | None = None,
    ) -> MemoryRetrievalResult:
        query = self._queries.for_targets(
            text=text,
            mode=mode,
            targets=targets,
            runtime=runtime,
            limit=limit,
            intent=intent,
        )
        return await self._retriever.retrieve(query)
