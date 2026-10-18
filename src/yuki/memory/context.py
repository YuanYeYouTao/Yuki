"""Authorized Memory retrieval and its fixed model grounding contract."""

from __future__ import annotations

from yuki.admin.models import RuntimeConfigSnapshot
from yuki.domain.messages import InboundMessage
from yuki.memory.authorized_scope import AuthorizedMemoryScope
from yuki.memory.enums import MemoryRetrievalMode
from yuki.memory.models import (
    MemoryEntityTarget,
    MemoryQueryIntent,
    MemoryRetrievalResult,
)
from yuki.memory.query import MemoryQueryBuilder
from yuki.memory.retrieval import MemoryRetriever
from yuki.memory.service import MemoryFactService

MEMORY_GROUNDING_RULE = (
    "当前请求不自动附带长期记忆。可见聊天和已有资料不足以回答人物、群或自己的既往事实时，"
    "按当前意图调用 search_memory；没有明确目标就不填 target，搜索当前主体有权读取的完整范围。"
    "长期记忆的 content 只支持其中明确写出的主张：不由偏好 X 推断排斥非 X，没有证据时不补充"
    "提及次数、最新状态或相反偏好。事件时间依据 content 中的明确日期或 occurred_at；updated_at 是"
    "存储更新时间，不能据此声称‘昨天’‘刚才’或事件发生日期。计划、答应和创建待办不能证明"
    "事情已完成。用户只要 N 件时至多给 N 件，一个 episode 含多件事时也只选所需的件数。"
    "空结果只表示本次查询未找到，不证明记忆不存在；返回条数有限或 truncated 都不代表全部"
    "存档。权限拒绝、查询故障不是无记录；限定范围被拒绝不能推断整个人的记忆均不可读，"
    "也不要自动换范围重试。严格日期无结果不得自动放宽。"
    "检索排名和相似度不能证明候选回答了问题，不把不相关或相近候选说成所问经历；"
    "整句检索没有直接证据时，可用关键原词或实质不同的查询补查，也可说明未找到。"
)


class MemoryContextService:
    """Compose deterministic retrieval for chat, tools, admin, and plugins."""

    def __init__(
        self,
        *,
        query_builder: MemoryQueryBuilder,
        retriever: MemoryRetriever,
        facts: MemoryFactService,
    ) -> None:
        self._queries = query_builder
        self._retriever = retriever
        self._facts = facts

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
