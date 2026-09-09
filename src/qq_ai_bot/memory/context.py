"""Entity-block projection for Memory V2 chat context."""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import replace
from typing import Any

from qq_ai_bot.admin.models import RuntimeConfigSnapshot
from qq_ai_bot.domain.messages import InboundMessage
from qq_ai_bot.memory.activation import MemoryActivationRepository
from qq_ai_bot.memory.enums import (
    MemoryAuthority,
    MemoryConflictState,
    MemoryContextMode,
    MemoryKind,
    MemoryRecallPurpose,
    MemoryRetrievalMode,
    MemoryTargetRole,
)
from qq_ai_bot.memory.metrics import MemoryLifecycleMetrics
from qq_ai_bot.memory.models import (
    MemoryContextBlock,
    MemoryEntityTarget,
    MemoryFact,
    MemoryQuery,
    MemoryQueryIntent,
    MemoryRetrievalHit,
    MemoryRetrievalResult,
)
from qq_ai_bot.memory.query import MemoryQueryBuilder, normalize_query_text
from qq_ai_bot.memory.receipt import MemoryRecallRepository, MemoryRecallTurn
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
                "_retrieval_score": hit.rerank_score,
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
        "category": hit.fact.category,
        "content": hit.fact.content,
        "confidence": hit.fact.confidence,
        "importance": hit.fact.importance,
    }
    if include_budget_metadata:
        context.update(
            {
                "_retrieval_score": hit.rerank_score,
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
    "长期记忆的 content 只支持其中明确写出的主张：不得由偏好 X 推断排斥非 X，不得在没有证据时"
    "补充提及次数、最新状态或相反偏好。只有 occurred_at 才是可用于正文的事件时间；updated_at 是"
    "存储更新时间，不能据此声称‘昨天’‘刚才’或事件发生日期。用户限制输出 N 条时至多输出 N 条；"
    "一个 episode 即使包含多件事，在用户只要一件时也只能选择其中一件。"
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
        activation: MemoryActivationRepository | None = None,
        receipts: MemoryRecallRepository | None = None,
        metrics: MemoryLifecycleMetrics | None = None,
    ) -> None:
        self._queries = query_builder
        self._retriever = retriever
        self._facts = facts
        self._activation = activation
        self._receipts = receipts
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

    async def retrieve_for_turn(
        self,
        *,
        inbound: InboundMessage,
        content: str,
        runtime: RuntimeConfigSnapshot,
        memory_mode: MemoryContextMode = MemoryContextMode.HYBRID,
        self_recall: bool = False,
        memory_intent: MemoryQueryIntent | None = None,
        requested_limit: int | None = None,
        neutral_ordering: bool = False,
    ) -> MemoryRetrievalResult:
        if memory_mode is MemoryContextMode.NONE:
            normalized = normalize_query_text(content)
            return MemoryRetrievalResult(
                blocks=(),
                hits=(),
                candidate_count=0,
                selected_count=0,
                query_hash=hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
                mode=MemoryRetrievalMode.RELEVANT,
                semantic_status="skipped",
            )
        if neutral_ordering:
            targets = await self.resolve_targets(inbound, runtime, self_recall=self_recall)
            query = self._queries.for_targets(
                text=content,
                mode=MemoryRetrievalMode.RELEVANT,
                targets=targets,
                runtime=runtime,
            ).model_copy(update={"semantic_enabled": False})
        else:
            query = await self._queries.build(
                inbound=inbound,
                content=content,
                runtime=runtime,
                memory_mode=memory_mode,
                self_recall=self_recall,
                memory_intent=memory_intent,
            )
            query = query.model_copy(
                update={
                    "limit_per_target": min(
                        query.limit_per_target,
                        runtime.memory.automatic_recall_per_target_limit,
                    )
                }
            )
        from qq_ai_bot.memory.runtime.query_plane import (
            MemoryQueryPlane,
            MemoryReadConsumer,
            MemoryReadRequest,
            ResolvedReadScope,
            apply_total_hit_limit,
        )

        self_target = None
        if (
            runtime.memory.self_enabled
            and query.intent is not None
            and not query.intent.self_recall
            and query.mode is MemoryRetrievalMode.RELEVANT
        ):
            self_target = next(
                (
                    target
                    for target in await self.resolve_targets(inbound, runtime, self_recall=True)
                    if target.role is MemoryTargetRole.CURRENT_SELF
                ),
                None,
            )
        result = await MemoryQueryPlane(self).read(
            MemoryReadConsumer.AUTOMATIC_CONTEXT,
            MemoryReadRequest(
                text=query.text,
                intent=query.intent,
                resolved_scope=ResolvedReadScope(targets=query.targets),
                automatic_self_target=self_target,
                neutral_ordering=neutral_ordering,
            ),
            runtime=runtime,
        )
        if requested_limit is not None and query.mode is MemoryRetrievalMode.OVERVIEW:
            return apply_total_hit_limit(result, requested_limit)
        return result

    async def retrieve_for_targets(
        self,
        *,
        content: str,
        targets: tuple[MemoryEntityTarget, ...],
        runtime: RuntimeConfigSnapshot,
        memory_mode: MemoryContextMode = MemoryContextMode.LEXICAL,
    ) -> MemoryRetrievalResult:
        """Retrieve host-resolved targets without inventing a message actor."""

        if memory_mode is MemoryContextMode.NONE:
            normalized = normalize_query_text(content)
            return MemoryRetrievalResult(
                blocks=(),
                hits=(),
                candidate_count=0,
                selected_count=0,
                query_hash=hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
                mode=MemoryRetrievalMode.RELEVANT,
                semantic_status="skipped",
            )
        query = self._queries.for_targets(
            text=content,
            mode=MemoryRetrievalMode.RELEVANT,
            targets=targets,
            runtime=runtime,
        )
        if memory_mode is MemoryContextMode.LEXICAL:
            query = query.model_copy(update={"semantic_enabled": False})
        return await self._retriever.retrieve(
            query,
            lexical_enabled=runtime.memory.retrieval_enabled,
            diversify=True,
        )

    @staticmethod
    def _limit_automatic_result(
        result: MemoryRetrievalResult,
        intent: MemoryQueryIntent | None,
        runtime: RuntimeConfigSnapshot,
    ) -> MemoryRetrievalResult:
        memory = runtime.memory
        purpose = intent.purpose if intent is not None else MemoryRecallPurpose.BACKGROUND
        if result.mode is MemoryRetrievalMode.OVERVIEW:
            total_limit = memory.automatic_recall_overview_limit
        elif purpose is MemoryRecallPurpose.BACKGROUND:
            total_limit = memory.automatic_recall_background_limit
        elif purpose is MemoryRecallPurpose.CONTINUATION:
            total_limit = memory.automatic_recall_continuation_limit
        else:
            total_limit = memory.automatic_recall_focused_limit

        calibrated = bool(
            memory.automatic_calibrated_profile
            and memory.automatic_calibrated_profile == result.embedding_profile
            and not result.semantic_degraded
            and memory.automatic_topic_threshold >= memory.automatic_background_threshold
        )
        topics: list[MemoryRetrievalHit] = []
        backgrounds: list[MemoryRetrievalHit] = []
        decisions: dict[int, str] = {}
        for hit in result.hits:
            exact = hit.selection_reason in {"memory_key_exact", "content_exact"}
            score = hit.semantic_score
            if exact or (
                calibrated and score is not None and score >= memory.automatic_topic_threshold
            ):
                topics.append(hit)
                decisions[hit.fact.id] = "topic"
            elif (
                calibrated
                and score is not None
                and score >= memory.automatic_background_threshold
                and hit.target.role is MemoryTargetRole.CURRENT_PERSON
            ):
                backgrounds.append(hit)
                decisions[hit.fact.id] = "background"
            else:
                decisions[hit.fact.id] = (
                    "rejected_relevance" if calibrated else "rejected_uncalibrated"
                )

        ordered = topics + (backgrounds[:1] if topics and len(topics) < total_limit else [])
        selected: list[MemoryRetrievalHit] = []
        per_target: dict[str, int] = {}
        selected_ids: set[int] = set()
        for hit in ordered:
            if len(selected) >= total_limit:
                break
            if hit.fact.id in selected_ids:
                continue
            target_key = hit.target.block_id
            if per_target.get(target_key, 0) >= memory.automatic_recall_per_target_limit:
                continue
            selected.append(
                hit.model_copy(
                    update={
                        "selection_reason": decisions[hit.fact.id],
                        "rank": len(selected) + 1,
                    }
                )
            )
            selected_ids.add(hit.fact.id)
            per_target[target_key] = per_target.get(target_key, 0) + 1

        by_target: dict[str, list[MemoryRetrievalHit]] = {}
        for hit in selected:
            by_target.setdefault(hit.target.block_id, []).append(hit)
        blocks = tuple(
            block.model_copy(update={"hits": tuple(by_target.get(block.target.block_id, ()))})
            for block in result.blocks
        )
        final_hits = tuple(selected)
        return result.model_copy(
            update={
                "blocks": blocks,
                "hits": final_hits,
                "selected_count": len(final_hits),
                "trace_hits": tuple(
                    hit.model_copy(
                        update={
                            "selection_reason": decisions.get(hit.fact.id, "not_selected"),
                        }
                    )
                    for hit in result.trace_hits
                ),
            }
        )

    async def search(
        self,
        *,
        text: str,
        mode: MemoryRetrievalMode,
        targets: tuple[MemoryEntityTarget, ...],
        runtime: RuntimeConfigSnapshot,
        limit: int | None = None,
        intent: MemoryQueryIntent | None = None,
        automatic: bool = False,
        automatic_self_target: MemoryEntityTarget | None = None,
        neutral_ordering: bool = False,
    ) -> MemoryRetrievalResult:
        query = self._queries.for_targets(
            text=text,
            mode=mode,
            targets=targets,
            runtime=runtime,
            limit=limit,
            intent=intent,
        )
        if not automatic:
            return await self._retriever.retrieve(query)
        if neutral_ordering:
            query = query.model_copy(update={"semantic_enabled": False})
        else:
            query = query.model_copy(
                update={
                    "limit_per_target": max(query.candidate_limit, query.semantic_candidate_limit),
                    "always_on_explicit_preference_limit": 0,
                    "targets": tuple(dict.fromkeys((*query.targets, automatic_self_target)))
                    if automatic_self_target is not None
                    else query.targets,
                }
            )
        if runtime.memory.retrieval_enabled:
            result = await self._retriever.retrieve(query)
        else:
            query = query.model_copy(
                update={
                    "targets": tuple(
                        target
                        for target in query.targets
                        if target.role
                        in {
                            MemoryTargetRole.CURRENT_PERSON,
                            MemoryTargetRole.CURRENT_SELF,
                            MemoryTargetRole.CURRENT_PERSON_GROUP,
                            MemoryTargetRole.CURRENT_GROUP,
                        }
                    ),
                    "limit_per_target": runtime.memory.context_limit_per_entity,
                }
            )
            result = await self._retriever.retrieve(query, lexical_enabled=False)
        return result if neutral_ordering else self._limit_automatic_result(result, intent, runtime)

    async def mark_injected(
        self,
        result: MemoryRetrievalResult,
        fact_ids: tuple[int, ...],
    ) -> int:
        selected = tuple(dict.fromkeys(fact_ids))
        updated = await self._facts.mark_injected(selected)
        self.metrics.record_recall_stage("injected", len(selected))
        latest = self._retriever.metrics.latest
        if latest is not None and latest.query_hash == result.query_hash:
            self._retriever.metrics.record_context_selected(
                replace(latest, context_selected_count=len(selected))
            )
        return updated

    async def record_recall(
        self,
        *,
        conversation_key: str,
        trigger_message_id: str,
        origin: str,
        intent: MemoryQueryIntent | None,
        result: MemoryRetrievalResult,
        injected_fact_ids: tuple[int, ...],
        runtime: RuntimeConfigSnapshot,
        consumer: str = "automatic_context",
    ) -> MemoryRecallTurn | None:
        if intent is None:
            return None
        self.metrics.record_intent(mode=intent.mode, purpose=intent.purpose)
        self.metrics.record_recall_stage("candidate", result.candidate_count)
        self.metrics.record_recall_stage("selected", result.selected_count)
        if not runtime.memory.recall_receipts_enabled or self._receipts is None:
            return MemoryRecallTurn(
                turn_id=str(uuid.uuid4()),
                injected_fact_ids=injected_fact_ids,
            )
        return await self._receipts.record_initial(
            conversation_key=conversation_key,
            trigger_message_id=trigger_message_id,
            origin=origin,
            intent=intent,
            result=result,
            injected_fact_ids=injected_fact_ids,
            retention_days=runtime.memory.recall_receipt_retention_days,
            consumer=consumer,
        )

    async def mark_attributed_used(
        self,
        turn_id: str,
        fact_ids: tuple[int, ...],
        *,
        evaluated_fact_ids: tuple[int, ...] | None = None,
    ) -> tuple[int, ...]:
        if self._receipts is None:
            self.metrics.record_recall_stage("used", len(fact_ids))
            return fact_ids
        recorded = await self._receipts.mark_attributed_used(
            turn_id, fact_ids, evaluated_fact_ids=evaluated_fact_ids
        )
        used = fact_ids if recorded is None else recorded
        self.metrics.record_recall_stage("used", len(used))
        return used

    async def set_attribution_outcome(self, turn_id: str, status: str, reason: str) -> None:
        if self._receipts is not None:
            await self._receipts.set_attribution_outcome(turn_id, status, reason)

    async def recover_pending_attribution(self) -> None:
        if self._receipts is not None:
            await self._receipts.recover_pending_attribution()

    async def mark_tool_injected(
        self,
        turn_id: str,
        fact_ids: tuple[int, ...],
    ) -> int:
        unique_ids = tuple(dict.fromkeys(fact_ids))
        updated = await self._facts.mark_injected(unique_ids)
        self.metrics.record_recall_stage("injected", len(unique_ids))
        if self._receipts is not None:
            await self._receipts.record_tool_injected(turn_id, unique_ids)
        return updated

    async def record_tool_read_outcome(self, turn_id: str, outcome: str) -> None:
        if self._receipts is not None:
            await self._receipts.record_tool_read_outcome(turn_id, outcome)

    async def reinforce_usage(
        self,
        *,
        turn_id: str,
        fact_ids: tuple[int, ...],
        intent: MemoryQueryIntent,
        runtime: RuntimeConfigSnapshot,
    ) -> tuple[int, ...]:
        if not runtime.memory.reinforcement_enabled or self._activation is None:
            self.metrics.record_reinforcement_skip(
                "disabled" if not runtime.memory.reinforcement_enabled else "activation_unavailable"
            )
            return ()
        alpha = {
            MemoryRecallPurpose.BACKGROUND: runtime.memory.reinforcement_alpha_background,
            MemoryRecallPurpose.CONTINUATION: (runtime.memory.reinforcement_alpha_continuation),
            MemoryRecallPurpose.RECALL: runtime.memory.reinforcement_alpha_recall,
            MemoryRecallPurpose.VERIFY: runtime.memory.reinforcement_alpha_verify,
            MemoryRecallPurpose.CORRECT: 0.0,
        }[intent.purpose]
        if alpha <= 0:
            self.metrics.record_reinforcement_skip("alpha_zero")
            return ()
        pending_lookup = (
            await self._receipts.pending_reinforcement(turn_id, fact_ids)
            if self._receipts is not None
            else fact_ids
        )
        receipt_bound = self._receipts is not None and pending_lookup is not None
        pending = pending_lookup if receipt_bound else fact_ids
        if not pending:
            self.metrics.record_reinforcement_skip("not_used")
            return ()
        policy_query = MemoryQuery(
            text="",
            normalized_text="",
            mode=MemoryRetrievalMode.RELEVANT,
            targets=(),
            candidate_limit=1,
            limit_per_target=1,
            always_on_explicit_preference_limit=0,
            query_term_limit=1,
            intent=intent,
            activation_half_life_episode_days=(runtime.memory.activation_half_life_episode_days),
            activation_half_life_fact_days=runtime.memory.activation_half_life_fact_days,
            activation_half_life_preference_days=(
                runtime.memory.activation_half_life_preference_days
            ),
            activation_half_life_explicit_days=(runtime.memory.activation_half_life_explicit_days),
        )
        reinforced = await self._activation.reinforce(
            pending,
            alpha=alpha,
            query=policy_query,
            receipt_turn_id=turn_id if receipt_bound else None,
        )
        if reinforced and self._receipts is not None:
            await self._receipts.mark_reinforced(turn_id, reinforced)
        self.metrics.record_recall_stage("reinforced", len(reinforced))
        if len(reinforced) < len(pending):
            self.metrics.record_reinforcement_skip(
                "fact_ineligible", len(pending) - len(reinforced)
            )
        return reinforced
