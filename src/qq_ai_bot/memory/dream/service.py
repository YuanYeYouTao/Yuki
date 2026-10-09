"""Plan exact-partition Dream clusters and commit model decisions atomically."""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from qq_ai_bot.config import Settings
from qq_ai_bot.memory.dream.models import (
    DreamAction,
    DreamCluster,
    DreamClusterPreview,
    DreamEvidenceInput,
    DreamInput,
    DreamMemoryInput,
    DreamOperationType,
    DreamOutput,
    DreamPlanStatistics,
    DreamRun,
    DreamRunMode,
)
from qq_ai_bot.memory.dream.planning import PreparedDreamCluster, prepare_clusters_from_facts
from qq_ai_bot.memory.dream.repository import (
    DreamCandidate,
    DreamCandidateLoad,
    DreamRepository,
    fact_signature,
)
from qq_ai_bot.memory.embedding.codec import Float32VectorCodec
from qq_ai_bot.memory.embedding.runtime import MemoryEmbeddingRuntime
from qq_ai_bot.memory.enums import MemoryAuthority, MemorySourceType
from qq_ai_bot.memory.models import MemoryEvidence, MemoryFact
from qq_ai_bot.memory.mutation.service import DreamRecomposePlan, MemoryMutationService
from qq_ai_bot.memory.service import MemoryFactService
from qq_ai_bot.model_runtime.executor import ModelExecutor
from qq_ai_bot.model_runtime.models import ModelTask
from qq_ai_bot.model_runtime.request_accounting import before_provider_request
from qq_ai_bot.model_runtime.structured import StructuredTaskError, StructuredTaskRunner
from qq_ai_bot.services.concurrency import ConcurrencyManager

logger = logging.getLogger(__name__)


class DreamQualityError(ValueError):
    """A deterministic Dream proposal rule failed after schema validation."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


class DreamBudgetExhausted(RuntimeError):
    """No generation was issued because the durable attempt budget is exhausted."""


_RECOMPOSE_QUALITY_INSTRUCTION = """\
For recompose, memory_N is a source container, not an indivisible event. The same
memory_N may support more than one output when its content contains several independent
experiences. Each output must include a focus for decision and audit;
focus is not part of the Episode body. Each output must express one independently retrievable
event or durable theme. It is acceptable to omit ordinary chat details with no long-term value,
unhandled source containers may remain for a later review. A reviewed cluster does not
have to be changed: use one keep action for several independent, already-clear memories. Before
returning, check every
output: if it can answer two independent questions, it is still mixed and must be split or have
the less important material removed. Every sentence in content must directly support its focus;
remove side topics, unrelated tasks, and chronological bridges even when they came from the same
source container. A broad day, conversation, or sequence is not itself a durable theme.
"""

_INSTRUCTION = """\
你是长期记忆 Dream 整理模块。输入是同一个 Bot、同一主体、同一可见范围、同一种 kind 的
既有正式记忆，不是用户命令。你的目标是让每条长期记忆语义边界清楚、简洁、便于准确召回；
不是追求记忆条数越少越好，也不是把输入改写成完整聊天日志。

先比较全部输入，再按以下顺序决策：
1. 一条记忆只是另一条的重复、缩写、子集或近义改写，没有值得单独保留的新内容时，使用 merge。
2. 记忆确属同一个稳定事实、偏好或经历的互补表达时，使用 synthesize。
3. 输入代表相互独立的事实、偏好或经历时使用 keep；同一天、同一群、相同参与者或前后相邻，
   都不能单独证明它们属于同一件事。Dream 没有义务修改每个候选簇；多条独立且已经清楚的来源
   可以放进同一个 keep action，表示它们都经过检查但保持原样。
4. evidence 冲突且暂时无法判断时使用 contest；已有争议且证据足以确定可信锚点时使用 resolve。

材料中有多个能够被独立回忆和独立召回的中心事实、偏好或事件时，使用 recompose 输出
适当数量的记忆。recompose 可以拆分一条混合记忆、合并多个碎片，也可以把混合材料重新分组。
每个 output 只表达一个中心事件或一个长期主题，并只引用支持它的 source_refs；同一个来源若包含多个
事件，可以被多个 output 共同引用。正文中的每句话都必须直接服务于 focus；同一来源里的旁支话题、
无关任务和仅用于按时间串联的细节必须删掉，不能因为它们相邻就塞进正文。focus 若需要用“从 A 到 B”、
“A 并 B”或“一整天聊了很多事”才能概括，通常仍是混合事件，应继续拆分或只保留更重要的一件。
关系变化、私密谈话或情感经历，与之后发生的工具任务、提醒、点单等功能性经历，回答的是不同问题；
即使它们在同一来源、同一晚或前后连续，也必须拆成不同 output，预算不足时舍弃较不重要的一件。
完成后逐句反查：删掉某句话若不改变 focus 所描述的核心经过、结果或认识，这句话就不应保留。

未处理的来源保持原样，可留给后续整理；显式keep表示已经审查并保持不变。
merge、synthesize、resolve 必须提供属于 source_refs 的 anchor_ref；不同 action 的 source_refs 不能
重叠。只有 synthesize 必须输出 content，并且可以输出 importance；keep、merge、contest、resolve
必须省略 content、importance 和 outputs。recompose 必须省略 anchor_ref、content 和 importance，
并通过 outputs 给出最终记忆。keep、contest 必须省略 anchor_ref。只能引用 memory_N 别名，
不能输出数据库 ID 或改变 scope/kind/key/category。

source_type=explicit 或 authority=explicit 的记忆是不可变锚点：不能被 synthesize、recompose、
contest、失效或作为 merge 的被吞并来源；自动重复记忆可以 merge 到唯一显式锚点。
两个显式锚点应分别 keep。所有结论和合成正文必须来自输入记忆与 evidence，不要发明新的经历。
SELF 合成正文
应保持第一人称和给定人格；人格只影响新正文的口吻，不应让你倾向于保留本可合并的碎片。
"""


@dataclass(frozen=True, slots=True)
class PreparedDreamPlan:
    statistics: DreamPlanStatistics
    clusters: tuple[PreparedDreamCluster, ...]
    snapshot_max_fact_id: int


async def prepare_full_core(
    *,
    settings: Settings,
    repository: DreamRepository,
    embeddings: MemoryEmbeddingRuntime,
) -> PreparedDreamPlan:
    if not settings.memory_embedding_enabled:
        raise RuntimeError("Memory Dream 需要启用 memory embedding")
    profile_id = embeddings.profile_id
    if profile_id is None:
        raise RuntimeError("Memory Dream embedding profile 尚未就绪")
    if embeddings.jobs is not None:
        await embeddings.jobs.reconcile()
    loaded = await repository.load_candidates(
        profile_id=profile_id,
        dimensions=embeddings.dimensions,
        documents=embeddings.documents,
    )
    planner = object.__new__(DreamService)
    planner._settings = settings
    planner._codec = Float32VectorCodec()
    clusters, isolated = await planner._clusters(loaded, incremental=False)
    statistics = DreamService._statistics(loaded, clusters=clusters, isolated=isolated)
    return PreparedDreamPlan(
        statistics,
        prepare_clusters_from_facts(
            planner._stored_clusters(clusters),
            tuple(item.fact for group in clusters for item in group),
        ),
        max((item.fact.id for item in loaded.candidates), default=0),
    )


async def plan_full_core(
    *,
    settings: Settings,
    repository: DreamRepository,
    embeddings: MemoryEmbeddingRuntime,
    actor_user_id: str,
    session: AsyncSession | None = None,
    prepared: PreparedDreamPlan | None = None,
) -> DreamRun:
    if prepared is None:
        if session is not None:
            raise ValueError("Dream plan must be prepared before opening a writer")
        prepared = await prepare_full_core(
            settings=settings, repository=repository, embeddings=embeddings
        )
    return await repository.create_run(
        mode=DreamRunMode.FULL,
        statistics=prepared.statistics,
        clusters=prepared.clusters,
        snapshot_max_fact_id=prepared.snapshot_max_fact_id,
        actor_user_id=actor_user_id,
        scheduled_slot=None,
        session=session,
    )


class DreamService:
    def __init__(
        self,
        *,
        settings: Settings,
        repository: DreamRepository,
        facts: MemoryFactService,
        mutations: MemoryMutationService,
        embeddings: MemoryEmbeddingRuntime,
        models: ModelExecutor,
        concurrency: ConcurrencyManager,
    ) -> None:
        self._settings = settings
        self._repository = repository
        self._facts = facts
        self._mutations = mutations
        self._embeddings = embeddings
        self._structured = StructuredTaskRunner(models)
        self._concurrency = concurrency
        self._codec = Float32VectorCodec()

    async def rollback_operation(self, public_id: str) -> bool:
        mutation_id = str(uuid.uuid4())
        affected_ids = await self._facts.repository.apply_evidence_write(
            lambda session: self._rollback_operation_in_session(
                public_id, mutation_id=mutation_id, session=session
            )
        )
        for fact_id in affected_ids:
            await self._facts.schedule_embedding(fact_id)
        return bool(affected_ids)

    async def _rollback_operation_in_session(
        self, public_id: str, *, mutation_id: str, session: AsyncSession
    ) -> tuple[int, ...]:
        affected_ids = await self._mutations.rollback_dream_operation(
            public_id=public_id,
            mutation_id=mutation_id,
            session=session,
        )
        for fact_id in affected_ids:
            fact = await self._facts.repository.get_fact(fact_id, session=session)
            if fact is not None:
                await self._repository.checkpoint_fact(fact, operation_id=None, session=session)
        return affected_ids

    async def rollback_run(self, public_id: str) -> int:
        if not await self._repository.mark_run_rolling_back(public_id):
            return 0
        operation_ids = await self._repository.committed_operation_ids(public_id)
        count = 0
        for operation_id in operation_ids:
            count += int(await self.rollback_operation(operation_id))
        await self._repository.mark_run_rolled_back(public_id)
        return count

    async def plan_full(
        self, *, actor_user_id: str, session: AsyncSession | None = None
    ) -> DreamRun:
        return await plan_full_core(
            settings=self._settings,
            repository=self._repository,
            embeddings=self._embeddings,
            actor_user_id=actor_user_id,
            session=session,
        )

    async def plan_incremental(self, *, scheduled_slot: str) -> DreamRun:
        loaded = await self._load()
        clusters, isolated = await self._clusters(loaded, incremental=True)
        attempted = await self._repository.attempted_fingerprints()
        unique = {self._candidate_cluster_fingerprint(items): items for items in clusters}
        clusters = tuple(
            unique[key]
            for key in sorted(
                unique,
                key=lambda key: (
                    key in attempted,
                    attempted[key].isoformat() if key in attempted else "",
                    tuple(item.fact.id for item in unique[key]),
                ),
            )
        )
        clusters = clusters[: self._settings.memory_dream_max_clusters_per_run]
        statistics = self._statistics(loaded, clusters=clusters, isolated=isolated)
        return await self._repository.create_run(
            mode=DreamRunMode.INCREMENTAL,
            statistics=statistics,
            clusters=prepare_clusters_from_facts(
                self._stored_clusters(clusters),
                tuple(item.fact for group in clusters for item in group),
            ),
            snapshot_max_fact_id=max((item.fact.id for item in loaded.candidates), default=0),
            actor_user_id=None,
            scheduled_slot=scheduled_slot,
        )

    async def preview_cluster(self, run_public_id: str, cluster_id: int) -> DreamClusterPreview:
        """Generate a read-only model proposal for one stored snapshot cluster."""

        run = await self._repository.get_run(run_public_id)
        cluster = await self._repository.cluster_for_run(run_public_id, cluster_id)
        if run is None or cluster is None:
            raise ValueError("没有找到该 Dream 候选簇")
        facts = await self._repository.cluster_facts(cluster)
        current_fingerprint = self._cluster_fingerprint(facts) if facts else ""
        if current_fingerprint != cluster.fingerprint:
            await self._repository.stale_previews(cluster.id)
        if (
            len(facts) != len(cluster.fact_ids)
            or self._cluster_fingerprint(facts) != cluster.fingerprint
        ):
            raise RuntimeError("Dream 候选簇快照已经变化，请重新 plan")
        payload, _ref_map, input_fingerprint = await self._input(facts)
        output, calls = await self._preview_decide(
            payload,
            self_memory=facts[0].scope_type.value == "self",
        )
        source_characters = sum(len(fact.content) for fact in facts)
        output_characters = self._output_characters(output)
        preview_public_id = await self._repository.save_preview(
            cluster_id=cluster.id,
            source_fingerprint=input_fingerprint,
            proposal=output,
            model_calls=calls,
            source_characters=source_characters,
            output_characters=output_characters,
        )
        return DreamClusterPreview(
            preview_public_id=preview_public_id,
            run_public_id=run.public_id,
            cluster_id=cluster.id,
            fact_ids=cluster.fact_ids,
            source_characters=source_characters,
            output_characters=output_characters,
            compression_ratio=(output_characters / source_characters if source_characters else 0.0),
            actions=output.actions,
        )

    async def process_cluster(
        self,
        run: DreamRun,
        cluster: DreamCluster,
    ) -> tuple[int, int, bool]:
        """Return actual model calls, operation count, and whether the snapshot stayed valid."""

        facts = await self._repository.cluster_facts(cluster)
        current_fingerprint = self._cluster_fingerprint(facts) if facts else ""
        if current_fingerprint != cluster.fingerprint:
            await self._repository.stale_previews(cluster.id)
        if (
            len(facts) != len(cluster.fact_ids)
            or self._cluster_fingerprint(facts) != cluster.fingerprint
        ):
            await self._repository.stale_previews(cluster.id)
            return 0, 0, False
        payload, ref_map, input_fingerprint = await self._input(facts)
        ready_preview = await self._repository.ready_preview(
            cluster_id=cluster.id,
            source_fingerprint=input_fingerprint,
        )
        preview_id: int | None = None
        if ready_preview is not None:
            preview_id, _preview_public_id, output = ready_preview
            calls = 0
        else:
            output, calls = await self._decide(
                payload,
                self_memory=facts[0].scope_type.value == "self",
                run=run,
                cluster=cluster,
            )
        operation_public_ids = tuple(str(uuid.uuid4()) for _ in output.actions)
        mutation_ids = tuple(str(uuid.uuid4()) for _ in output.actions)
        embedding_ids, operation_count = await self._facts.repository.apply_evidence_write(
            lambda session: self._commit_cluster_decision(
                run=run,
                cluster=cluster,
                ref_map=ref_map,
                input_fingerprint=input_fingerprint,
                output=output,
                preview_id=preview_id,
                operation_public_ids=operation_public_ids,
                mutation_ids=mutation_ids,
                session=session,
            )
        )
        for fact_id in embedding_ids:
            await self._facts.schedule_embedding(fact_id)
        return calls, operation_count, True

    async def _commit_cluster_decision(
        self,
        *,
        run: DreamRun,
        cluster: DreamCluster,
        ref_map: dict[str, MemoryFact],
        input_fingerprint: str,
        output: DreamOutput,
        preview_id: int | None,
        operation_public_ids: tuple[str, ...],
        mutation_ids: tuple[str, ...],
        session: AsyncSession,
    ) -> tuple[set[int], int]:
        embedding_ids: set[int] = set()
        operation_count = 0
        current_map: dict[str, MemoryFact] = {}
        for ref, snapshot in ref_map.items():
            current = await self._facts.repository.get_fact(snapshot.id, session=session)
            if (
                current is None
                or fact_signature(current) != fact_signature(snapshot)
                or self._mutations._dream_partition(current)
                != self._mutations._dream_partition(snapshot)
                or current.review_state != snapshot.review_state
            ):
                raise RuntimeError("dream_cluster_stale")
            current_map[ref] = current
        await self._facts.prepare_evidence_write(
            tuple(fact.id for fact in current_map.values()),
            targets=tuple(current_map.values()),
            session=session,
        )
        # The model and any saved preview consumed these exact readable sources.
        # Equal evidence counts do not prove that the original evidence is live.
        _, _, current_input_fingerprint = await self._input(
            tuple(current_map.values()), session=session
        )
        if current_input_fingerprint != input_fingerprint:
            raise RuntimeError("dream_input_snapshot_changed")
        for action in output.actions:
            sources = tuple(current_map[ref] for ref in action.source_refs)
            if action.operation is DreamOperationType.RECOMPOSE:
                referenced = {ref for item in action.outputs for ref in item.source_refs}
                sources = tuple(current_map[ref] for ref in action.source_refs if ref in referenced)
            anchor = self._anchor(action, sources, current_map)
            await self._mutations.prepare_dream_evidence(
                sources,
                anchor_fact_id=anchor.id if anchor is not None else None,
                recompose_outputs=tuple(
                    DreamRecomposePlan(
                        source_facts=tuple(current_map[ref] for ref in item.source_refs),
                        content=item.content,
                        importance=item.importance,
                    )
                    for item in action.outputs
                ),
                session=session,
            )
        used: set[str] = set()
        for action_index, action in enumerate(output.actions, start=1):
            sources = tuple(current_map[ref] for ref in action.source_refs if ref in current_map)
            if len(sources) != len(action.source_refs):
                raise ValueError("dream output referenced an unknown memory alias")
            if used.intersection(action.consumed_source_refs):
                raise ValueError("dream output reused a memory alias")
            used.update(action.consumed_source_refs)
            if action.operation is DreamOperationType.RECOMPOSE:
                referenced = {ref for item in action.outputs for ref in item.source_refs}
                sources = tuple(current_map[ref] for ref in action.source_refs if ref in referenced)
            anchor = self._anchor(action, sources, current_map)
            recompose_outputs = tuple(
                DreamRecomposePlan(
                    source_facts=tuple(current_map[ref] for ref in item.source_refs),
                    content=item.content,
                    importance=item.importance,
                )
                for item in action.outputs
            )
            operation = await self._repository.create_operation(
                cluster_id=cluster.id,
                action_index=action_index,
                operation_type=action.operation,
                source_facts=sources,
                anchor_fact_id=anchor.id if anchor is not None else None,
                session=session,
                decision_focuses=tuple(item.focus for item in action.outputs),
                public_id=operation_public_ids[action_index - 1],
            )
            result = await self._mutations.mutate_dream(
                dream_operation_id=operation.id,
                operation_type=action.operation,
                source_facts=sources,
                anchor_fact_id=anchor.id if anchor is not None else None,
                content=action.content,
                importance=action.importance,
                recompose_outputs=recompose_outputs,
                bot_user_id=cluster.bot_user_id,
                run_public_id=run.public_id,
                session=session,
                mutation_id=mutation_ids[action_index - 1],
            )
            if result.changed:
                embedding_ids.update(source.id for source in sources)
            loaded_outputs: list[MemoryFact] = []
            for fact_id in result.output_fact_ids:
                loaded_output = await self._facts.repository.get_fact(fact_id, session=session)
                if loaded_output is not None:
                    loaded_outputs.append(loaded_output)
            output_facts = tuple(loaded_outputs)
            if len(output_facts) != len(result.output_fact_ids):
                raise RuntimeError("dream output fact disappeared before commit")
            latest_sources: dict[int, MemoryFact] = {}
            for source in sources:
                latest = await self._facts.repository.get_fact(source.id, session=session)
                if latest is not None:
                    latest_sources[source.id] = latest
            await self._repository.commit_operation(
                operation.id,
                output_fact_id=result.output_fact_id,
                output_results=tuple((fact.id, fact_signature(fact)) for fact in output_facts),
                added_evidence_ids=result.added_evidence_ids,
                added_relation_ids=result.added_relation_ids,
                result_signature=(fact_signature(output_facts[0]) if output_facts else None),
                source_signatures={
                    fact_id: fact_signature(latest) for fact_id, latest in latest_sources.items()
                },
                session=session,
            )
            for latest in latest_sources.values():
                await self._repository.checkpoint_fact(
                    latest, operation_id=operation.id, session=session
                )
            for output_fact in output_facts:
                await self._repository.checkpoint_fact(
                    output_fact, operation_id=operation.id, session=session
                )
                embedding_ids.add(output_fact.id)
            operation_count += 1
        if preview_id is not None:
            await self._repository.mark_preview_applied(preview_id, session=session)
        return embedding_ids, operation_count

    async def _load(self) -> DreamCandidateLoad:
        if not self._settings.memory_embedding_enabled:
            raise RuntimeError("Memory Dream 需要启用 memory embedding")
        profile_id = self._embeddings.profile_id
        if profile_id is None:
            raise RuntimeError("Memory Dream embedding profile 尚未就绪")
        if self._embeddings.jobs is not None:
            await self._embeddings.jobs.reconcile()
        return await self._repository.load_candidates(
            profile_id=profile_id,
            dimensions=self._embeddings.dimensions,
            documents=self._embeddings.documents,
        )

    async def _clusters(
        self,
        loaded: DreamCandidateLoad,
        *,
        incremental: bool,
    ) -> tuple[tuple[tuple[DreamCandidate, ...], ...], tuple[DreamCandidate, ...]]:
        checkpoints = await self._repository.checkpoint_map() if incremental else {}
        changed = {
            item.fact.id
            for item in loaded.candidates
            if not incremental or checkpoints.get(item.fact.id) != item.signature
        }
        partitions: dict[tuple[object, ...], list[DreamCandidate]] = defaultdict(list)
        for candidate in loaded.candidates:
            partitions[candidate.partition_identity].append(candidate)
        clusters: list[tuple[DreamCandidate, ...]] = []
        clustered_ids: set[int] = set()
        for partition in sorted(partitions, key=repr):
            rows = sorted(partitions[partition], key=lambda item: item.fact.id)
            by_id = {item.fact.id: item for item in rows}
            similarities: dict[tuple[int, int], float] = {}
            for index, left in enumerate(rows):
                for right in rows[index + 1 :]:
                    similarities[(left.fact.id, right.fact.id)] = self._codec.dot(
                        left.vector, right.vector
                    )
            remaining = set(by_id)
            while remaining:
                seed = max(
                    remaining,
                    key=lambda fact_id: (
                        sum(
                            self._similarity(fact_id, other, similarities)
                            >= self._settings.memory_dream_similarity_threshold
                            for other in remaining
                            if other != fact_id
                        ),
                        -fact_id,
                    ),
                )
                group = [seed]
                remaining.remove(seed)
                candidates = sorted(
                    remaining,
                    key=lambda fact_id: (
                        -self._similarity(seed, fact_id, similarities),
                        fact_id,
                    ),
                )
                for candidate_id in candidates:
                    if len(group) >= self._settings.memory_dream_max_cluster_size:
                        break
                    if all(
                        self._similarity(candidate_id, member, similarities)
                        >= self._settings.memory_dream_similarity_threshold
                        for member in group
                    ):
                        group.append(candidate_id)
                        remaining.remove(candidate_id)
                ids = tuple(sorted(group))
                if not incremental or changed.intersection(ids):
                    cluster = tuple(by_id[fact_id] for fact_id in ids)
                    clusters.append(cluster)
                    clustered_ids.update(ids)
        isolated = tuple(
            item
            for item in loaded.candidates
            if item.fact.id in changed and item.fact.id not in clustered_ids
        )
        clusters.sort(key=lambda items: tuple(item.fact.id for item in items))
        return tuple(clusters), isolated

    @staticmethod
    def _similarity(
        left: int,
        right: int,
        similarities: dict[tuple[int, int], float],
    ) -> float:
        if left == right:
            return 1.0
        return similarities[(min(left, right), max(left, right))]

    def _stored_clusters(
        self,
        clusters: tuple[tuple[DreamCandidate, ...], ...],
    ) -> tuple[tuple[str, str, str, str, tuple[int, ...], str], ...]:
        rows = []
        for cluster in clusters:
            fact_ids = tuple(item.fact.id for item in cluster)
            partition_key = hashlib.sha256(repr(cluster[0].partition_identity).encode()).hexdigest()
            fingerprint = self._candidate_cluster_fingerprint(cluster)
            cluster_key = hashlib.sha256(
                f"{partition_key}:{','.join(map(str, fact_ids))}:{fingerprint}".encode()
            ).hexdigest()
            rows.append(
                (
                    cluster_key,
                    partition_key,
                    cluster[0].bot_user_id,
                    cluster[0].fact.kind.value,
                    fact_ids,
                    fingerprint,
                )
            )
        return tuple(rows)

    @staticmethod
    def _candidate_cluster_fingerprint(cluster: tuple[DreamCandidate, ...]) -> str:
        return hashlib.sha256(
            ":".join(
                item.signature for item in sorted(cluster, key=lambda row: row.fact.id)
            ).encode()
        ).hexdigest()

    @staticmethod
    def _cluster_fingerprint(facts: tuple[MemoryFact, ...]) -> str:
        return hashlib.sha256(
            ":".join(
                fact_signature(item) for item in sorted(facts, key=lambda row: row.id)
            ).encode()
        ).hexdigest()

    @staticmethod
    def _statistics(
        loaded: DreamCandidateLoad,
        *,
        clusters: tuple[tuple[DreamCandidate, ...], ...],
        isolated: tuple[DreamCandidate, ...],
    ) -> DreamPlanStatistics:
        return DreamPlanStatistics(
            eligible_facts=loaded.eligible_facts,
            ready_facts=len(loaded.candidates),
            missing_embeddings=loaded.missing_embeddings,
            ambiguous_bot_facts=loaded.ambiguous_bot_facts,
            partitions=len({item.partition_identity for item in loaded.candidates}),
            candidate_clusters=len(clusters),
            isolated_facts=len(isolated),
            estimated_model_calls=len(clusters),
        )

    async def _input(
        self, facts: tuple[MemoryFact, ...], *, session: AsyncSession | None = None
    ) -> tuple[DreamInput, dict[str, MemoryFact], str]:
        ref_map = {f"memory_{index}": fact for index, fact in enumerate(facts, start=1)}
        remaining = self._settings.memory_dream_max_input_characters
        rows: list[DreamMemoryInput] = []
        evidence_proofs: list[tuple[int, tuple[dict[str, Any], ...]]] = []
        for ref, fact in ref_map.items():
            content = fact.content
            remaining -= len(content)
            evidence_rows = await self._facts.repository.list_evidence(
                fact.id, limit=None, session=session
            )
            selected = self._select_evidence(evidence_rows)
            evidence_proofs.append(
                (fact.id, tuple(item.model_dump(mode="json") for item in selected))
            )
            evidence: list[DreamEvidenceInput] = []
            for item in selected:
                if remaining <= 0:
                    break
                excerpt = item.excerpt[
                    : min(
                        self._settings.memory_dream_evidence_excerpt_characters,
                        remaining,
                    )
                ]
                remaining -= len(excerpt)
                evidence.append(
                    DreamEvidenceInput(
                        occurred_at=item.created_at,
                        relation=item.relation.value,
                        excerpt=excerpt,
                    )
                )
            rows.append(
                DreamMemoryInput(
                    ref=ref,
                    kind=fact.kind.value,
                    category=fact.category,
                    memory_key=fact.memory_key,
                    content=content,
                    importance=fact.importance,
                    confidence=fact.confidence,
                    source_type=fact.source_type.value,
                    authority=fact.authority.value,
                    status=fact.status.value,
                    conflict_state=fact.conflict_state.value,
                    valid_from=fact.valid_from,
                    valid_until=fact.valid_until,
                    evidence=tuple(evidence),
                )
            )
        first = facts[0]
        payload = DreamInput(
            scope_type=first.scope_type.value,
            subject_user_id=first.subject_user_id,
            group_id=first.group_id,
            visibility_type=(
                first.visibility_type.value if first.visibility_type is not None else None
            ),
            visibility_user_id=first.visibility_user_id,
            visibility_group_id=first.visibility_group_id,
            kind=first.kind.value,
            memories=tuple(rows),
        )
        fitted = self._fit_input(payload)
        proof = {
            "input": fitted.model_dump(mode="json"),
            "evidence": evidence_proofs,
            "facts": tuple(
                (fact_signature(fact), self._mutations._dream_partition(fact), fact.review_state)
                for fact in facts
            ),
        }
        fingerprint = hashlib.sha256(
            json.dumps(proof, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return fitted, ref_map, fingerprint

    @staticmethod
    def _output_characters(output: DreamOutput) -> int:
        return sum(
            len(action.content or "") + sum(len(item.content) for item in action.outputs)
            for action in output.actions
        )

    def _fit_input(self, payload: DreamInput) -> DreamInput:
        maximum = self._settings.memory_dream_max_input_characters
        current = payload
        while len(current.model_dump_json()) > maximum:
            overflow = len(current.model_dump_json()) - maximum
            rows = list(current.memories)
            changed = False
            for row_index in range(len(rows) - 1, -1, -1):
                row = rows[row_index]
                evidence = list(row.evidence)
                for evidence_index in range(len(evidence) - 1, -1, -1):
                    excerpt = evidence[evidence_index].excerpt
                    if not excerpt:
                        continue
                    cut = min(len(excerpt), max(1, overflow))
                    evidence[evidence_index] = evidence[evidence_index].model_copy(
                        update={"excerpt": excerpt[:-cut]}
                    )
                    overflow -= cut
                    changed = True
                    if overflow <= 0:
                        break
                row = row.model_copy(update={"evidence": tuple(evidence)})
                rows[row_index] = row
                if overflow <= 0:
                    break
            if not changed:
                raise DreamQualityError(
                    "dream_input_too_large", "complete source facts exceed input budget"
                )
            current = current.model_copy(update={"memories": tuple(rows)})
        return current

    def _select_evidence(self, rows: tuple[MemoryEvidence, ...]) -> tuple[MemoryEvidence, ...]:
        limit = self._settings.memory_dream_evidence_per_fact
        if not rows or limit <= 0:
            return ()
        ordered = tuple(sorted(rows, key=lambda item: item.created_at))
        if len(ordered) <= limit:
            return ordered
        if limit == 1:
            return (ordered[-1],)
        if limit == 2:
            return (ordered[0], ordered[-1])
        middle = ordered[1:-1]
        return (ordered[0], *middle[-(limit - 2) :], ordered[-1])

    async def _decide(
        self,
        payload: DreamInput,
        *,
        self_memory: bool,
        run: DreamRun,
        cluster: DreamCluster,
    ) -> tuple[DreamOutput, int]:
        instruction = self._instruction(self_memory=self_memory, payload=payload)
        calls = 0

        async def reserve() -> None:
            nonlocal calls
            if not await self._reserve_model_call(run, cluster):
                raise DreamBudgetExhausted("memory_dream_model_call_budget_exhausted")
            calls += 1

        result = await self._run_model(instruction, payload, before_dispatch=reserve)
        return result, calls

    async def _preview_decide(
        self,
        payload: DreamInput,
        *,
        self_memory: bool,
    ) -> tuple[DreamOutput, int]:
        instruction = self._instruction(self_memory=self_memory, payload=payload)
        calls = 0

        async def count() -> None:
            nonlocal calls
            calls += 1

        result = await self._run_model(instruction, payload, before_dispatch=count)
        return result, calls

    @staticmethod
    def _quality_reason(error: StructuredTaskError | ValueError) -> str:
        if isinstance(error, StructuredTaskError):
            return error.reason_code
        if isinstance(error, DreamQualityError):
            return error.code
        return "dream_quality_validation_failed"

    @staticmethod
    def _quality_detail(error: StructuredTaskError | ValueError) -> str:
        if isinstance(error, StructuredTaskError):
            return error.detail
        if isinstance(error, DreamQualityError):
            return error.detail
        return str(error)

    def _instruction(self, *, self_memory: bool, payload: DreamInput) -> str:
        instruction = f"{_INSTRUCTION}\n{_RECOMPOSE_QUALITY_INSTRUCTION}"
        if self_memory:
            instruction += (
                f"\n【{self._settings.bot_display_name} 共享核心人格】\n"
                f"{self._settings.bot_persona}\n"
                "SELF 记忆应保持第一人称和这一人格的自然口吻。"
            )
        return instruction

    def _structured_input(self, payload: DreamInput) -> dict[str, Any]:
        """Keep cluster-specific sizes in the user input, after the reusable instruction."""

        return payload.model_dump(mode="json", exclude_none=True, exclude_defaults=True)

    async def _reserve_model_call(self, run: DreamRun, cluster: DreamCluster) -> bool:
        return await self._repository.reserve_model_call(
            run_public_id=run.public_id,
            cluster_id=cluster.id,
            maximum=self._settings.memory_dream_max_model_calls_per_run,
        )

    async def _run_model(
        self,
        instruction: str,
        payload: DreamInput,
        *,
        before_dispatch: Callable[[], Awaitable[None]],
    ) -> DreamOutput:
        token = before_provider_request.set(before_dispatch)
        try:
            return await self._concurrency.run_llm(
                "memory-dream",
                lambda: self._structured.run(
                    task=ModelTask.MEMORY_DREAM,
                    instruction=instruction,
                    structured_input=self._structured_input(payload),
                    output_model=DreamOutput,
                    temperature=0.1,
                    max_output_tokens=self._settings.memory_dream_max_output_tokens,
                    validation_retries=1,
                    validation_repair_hint=(
                        "Correct the reported fields using only supplied source aliases; "
                        "unhandled memories can remain unchanged."
                    ),
                ),
                translate_cancellation=False,
            )
        finally:
            before_provider_request.reset(token)

    def _anchor(
        self,
        action: DreamAction,
        sources: tuple[MemoryFact, ...],
        ref_map: dict[str, MemoryFact],
    ) -> MemoryFact | None:
        if action.operation not in {
            DreamOperationType.MERGE,
            DreamOperationType.SYNTHESIZE,
            DreamOperationType.RECOMPOSE,
            DreamOperationType.RESOLVE,
        }:
            return None
        explicit = tuple(
            item
            for item in sources
            if item.source_type is MemorySourceType.EXPLICIT
            or item.authority is MemoryAuthority.EXPLICIT
        )
        if explicit:
            return explicit[0]
        if action.operation is DreamOperationType.RESOLVE and action.anchor_ref is not None:
            return ref_map[action.anchor_ref]
        return self._mutations.select_dream_anchor(sources)
