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

MEMORY_GROUNDING_RULE = (
    "当前请求不自动附带长期记忆。可见聊天和已有资料不足以回答人物、群或自己的既往事实时，"
    "按当前意图调用 search_memory；没有明确目标就不填 target，搜索当前主体有权读取的完整范围。"
    "按 content 中明确写出的事实回答。例如‘喜欢咖啡’只说明这项偏好，不代表‘不喝茶’；"
    "‘答应周末交稿’是计划，不能当作已经交稿。提及次数、最新状态也需有对应记录。"
    "事件时间依据 content 中的明确日期或 occurred_at；updated_at 只是存储更新时间，"
    "不表示事情发生在‘昨天’或‘刚才’。用户只要 N 件时至多给 N 件，"
    "一个 episode 含多件事时也只选所需的件数。"
    "空结果只表示本次查询未找到，不证明记忆不存在；返回条数有限或 truncated 都不代表全部"
    "存档。权限拒绝或查询故障不是空结果，按实际原因说明；某一范围被拒绝不代表整个人的记忆均不可读，"
    "查询范围仍按实际问题选择。严格日期无结果时保持原日期范围。"
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
    ) -> None:
        self._queries = query_builder
        self._retriever = retriever

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
