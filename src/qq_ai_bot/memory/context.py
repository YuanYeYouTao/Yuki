"""Authorized Memory retrieval and its fixed model grounding contract."""

from __future__ import annotations

from qq_ai_bot.admin.models import RuntimeConfigSnapshot
from qq_ai_bot.domain.messages import InboundMessage
from qq_ai_bot.memory.authorized_scope import AuthorizedMemoryScope
from qq_ai_bot.memory.enums import MemoryRetrievalMode
from qq_ai_bot.memory.metrics import MemoryLifecycleMetrics
from qq_ai_bot.memory.models import (
    MemoryEntityTarget,
    MemoryQueryIntent,
    MemoryRetrievalResult,
)
from qq_ai_bot.memory.query import MemoryQueryBuilder
from qq_ai_bot.memory.retrieval import MemoryRetriever
from qq_ai_bot.memory.service import MemoryFactService

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
