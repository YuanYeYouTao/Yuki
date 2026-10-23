"""Unified explicit person-memory administration."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from yuki.admin.audit import AdminAuditService
from yuki.admin.config_service import RuntimeConfigService
from yuki.admin.models import AdminActor
from yuki.config import Settings
from yuki.memory.audit import MemoryAuditService
from yuki.memory.context import MemoryContextService
from yuki.memory.dream.models import DreamClusterPreview, DreamRun, DreamRunPage
from yuki.memory.dream.worker import DreamWorker
from yuki.memory.embedding.models import MemoryEmbeddingHealth
from yuki.memory.embedding.runtime import MemoryEmbeddingRuntime
from yuki.memory.enums import (
    MemoryAuthority,
    MemoryInvalidationReason,
    MemoryKind,
    MemoryScopeType,
    MemorySourceType,
    MemoryStatus,
    MemoryTargetRole,
)
from yuki.memory.fts import SQLiteMemoryFTSIndex
from yuki.memory.maintenance import MemoryMaintenanceWorker
from yuki.memory.models import (
    MemoryConsistencyHealth,
    MemoryEntityTarget,
    MemoryEvidence,
    MemoryEvidenceCreate,
    MemoryFact,
    MemoryFactCreate,
    MemoryFactStateEvent,
    MemoryIndexHealth,
    MemoryRetrievalResult,
)
from yuki.memory.mutation.models import (
    MemoryDecisionActorType,
    MemoryMutationContext,
    MemoryMutationOperation,
    MemoryMutationRequest,
    MemoryMutationResult,
    MemoryMutationTarget,
)
from yuki.memory.mutation.service import MemoryMutationService
from yuki.memory.query import MemoryQueryBuilder
from yuki.memory.retrieval import MemoryRetriever
from yuki.memory.runtime.query_plane import (
    MemoryQueryPlane,
    MemoryReadConsumer,
    MemoryReadRequest,
    ResolvedReadScope,
)
from yuki.memory.self_reflection.worker import SelfReflectionWorker
from yuki.memory.service import MemoryFactService
from yuki.memory.subjects import ResolvedSubject
from yuki.memory.targets import MemoryTargetResolver
from yuki.memory.validation import normalize_memory_text
from yuki.persistence.people_repository import PeopleRepository
from yuki.persistence.repositories import EventLedgerRepository


@dataclass(frozen=True, slots=True)
class MemoryPreferenceTrigger:
    """Already-authorized memory mutation correlation. Not an AdminActor.

    Role booleans are historical snapshots. Execution resolves the current
    Person qualification; plugins cannot supply authority.
    """

    user_id: str
    bot_user_id: str
    trigger_message_id: str
    conversation_key: str
    decision_actor_type: str = "admin"
    decision_actor_id: str | None = None
    trigger_event_id: int | None = None
    canonical_conversation_id: str | None = None
    ingress_presence_id: str | None = None
    actor_is_superuser: bool = False
    person_id: str | None = None
    operation_id: str | None = None


class MemoryAdminService:
    """Manage explicit memories and bounded automatic-memory retention rules."""

    def __init__(
        self,
        *,
        settings: Settings,
        memories: MemoryFactService,
        audit: AdminAuditService,
        memory_context: MemoryContextService | None = None,
        memory_index: SQLiteMemoryFTSIndex | None = None,
        runtime_config: RuntimeConfigService | None = None,
        memory_embeddings: MemoryEmbeddingRuntime | None = None,
        fact_audit: MemoryAuditService | None = None,
        maintenance: MemoryMaintenanceWorker | None = None,
        mutations: MemoryMutationService | None = None,
        ledger: EventLedgerRepository | None = None,
        self_reflection: SelfReflectionWorker | None = None,
        dream: DreamWorker | None = None,
    ) -> None:
        self._settings = settings
        self._memories = memories
        self._audit = audit
        database = memories.repository.database
        self._database = database
        self._memory_index = memory_index or SQLiteMemoryFTSIndex(database)
        self._memory_context = memory_context or MemoryContextService(
            query_builder=MemoryQueryBuilder(MemoryTargetResolver(PeopleRepository(database))),
            retriever=MemoryRetriever(
                repository=memories.repository,
                lexical_index=self._memory_index,
            ),
        )
        self._runtime_config = runtime_config or RuntimeConfigService(
            settings=settings,
            database=database,
        )
        self._memory_embeddings = memory_embeddings
        self._fact_audit = fact_audit or MemoryAuditService(memories.repository)
        self._maintenance = maintenance
        self._mutations = mutations
        self._ledger = ledger
        self._self_reflection = self_reflection
        self._dream = dream

    async def list_memories(
        self,
        actor: AdminActor,
        target: str,
        *,
        target_person_id: str | None = None,
    ) -> tuple[MemoryFact, ...]:
        async with self._database.read_session() as session:
            await session.execute(text("BEGIN"))
            target_person_id = await self._require_authorized_person_target(
                actor, target, person_id=target_person_id, session=session
            )
            return await self._memories.list_person(person_id=target_person_id, session=session)

    async def list_explicit_preferences(
        self,
        actor: AdminActor | MemoryPreferenceTrigger,
        target: str,
        *,
        target_person_id: str,
    ) -> tuple[MemoryFact, ...]:
        async with self._database.read_session() as session:
            await session.execute(text("BEGIN"))
            person_id = await self._require_authorized_person_target(
                actor, target, person_id=target_person_id, session=session
            )
            return await self._memories.list_preferences(person_id=person_id, session=session)

    async def set_explicit_preference(
        self,
        trigger: MemoryPreferenceTrigger,
        target: str,
        key: str,
        value: str,
        *,
        existing: MemoryFact | None,
        target_person_id: str | None = None,
    ) -> MemoryFact:
        """Route deterministic preference writes through the mutation boundary."""

        target_person_id = await self._require_authorized_person_target(
            trigger, target, person_id=target_person_id
        )
        mutation = await self._apply_mutation(
            trigger,
            target=ResolvedSubject(
                MemoryScopeType.PERSON,
                target,
                None,
                canonical_subject_person_id=target_person_id,
            ),
            operation=(
                MemoryMutationOperation.CORRECT
                if existing is not None
                else MemoryMutationOperation.CREATE
            ),
            fact_id=existing.id if existing is not None else None,
            new_content=value,
            memory_key=key,
            category="preference",
            kind=MemoryKind.PREFERENCE,
            reason="deterministic_preference_set",
            confidence=1.0,
            importance=4,
        )
        if mutation is None:
            raise RuntimeError("memory mutation service is unavailable")
        row = await self._mutation_fact(mutation)
        assert row is not None
        return row

    async def delete_explicit_preference(
        self,
        trigger: MemoryPreferenceTrigger,
        target: str,
        existing: MemoryFact,
        *,
        target_person_id: str | None = None,
    ) -> bool:
        """Route deterministic preference deletion through the mutation boundary."""

        target_person_id = await self._require_authorized_person_target(
            trigger, target, person_id=target_person_id
        )
        mutation = await self._apply_mutation(
            trigger,
            target=ResolvedSubject(
                MemoryScopeType.PERSON,
                target,
                None,
                canonical_subject_person_id=target_person_id,
            ),
            operation=MemoryMutationOperation.INVALIDATE,
            fact_id=existing.id,
            reason="deterministic_preference_delete",
        )
        if mutation is None:
            raise RuntimeError("memory mutation service is unavailable")
        return mutation.ok

    async def add_memory(
        self,
        actor: AdminActor | MemoryPreferenceTrigger,
        target: str,
        content: str,
        *,
        evidence: MemoryEvidenceCreate | None = None,
        target_person_id: str | None = None,
    ) -> MemoryFact:
        target_person_id = await self._require_authorized_person_target(
            actor, target, person_id=target_person_id
        )
        normalized = " ".join(content.split()).strip()
        if not normalized:
            raise ValueError("记忆内容不能为空")
        started = time.perf_counter()
        mutation = await self._apply_mutation(
            actor,
            target=ResolvedSubject(
                MemoryScopeType.PERSON,
                target,
                None,
                canonical_subject_person_id=target_person_id,
            ),
            operation=MemoryMutationOperation.CREATE,
            new_content=normalized,
            memory_key=None,
            category="explicit",
            kind=MemoryKind.FACT,
            reason="deterministic_memory_add",
            confidence=1.0,
            importance=5,
        )
        if mutation is not None:
            row = await self._mutation_fact(mutation)
            assert row is not None
            return row

        draft = MemoryFactCreate(
            scope_type=MemoryScopeType.PERSON,
            subject_user_id=target,
            canonical_subject_person_id=target_person_id,
            kind=MemoryKind.FACT,
            memory_key=f"explicit-{uuid4()}",
            category="explicit",
            content=normalized,
            importance=5,
            confidence=1,
            source_type=MemorySourceType.EXPLICIT,
            authority=MemoryAuthority.EXPLICIT,
        )
        prepared = await self._memories.prepare_write((), targets=(draft,))

        async def write(session: AsyncSession) -> MemoryFact:
            await self._require_authorized_person_target(
                actor, target, person_id=target_person_id, session=session
            )
            row = await self._memories.remember(
                draft,
                evidence=evidence,
                session=session,
            )
            await self._audit.record(
                actor=actor,
                capability="memory",
                operation="add",
                target_type="user",
                target_id=target,
                before=None,
                after={"memory_id": row.id, "content": normalized},
                success=True,
                duration_seconds=time.perf_counter() - started,
                session=session,
            )
            return row

        async with self._memories.repository.write_session(prepared) as writer:
            row = await write(writer)
        await self._memories.schedule_embedding(row.id)
        return row

    async def update_memory(
        self,
        actor: AdminActor,
        target: str,
        memory_id: int,
        content: str,
        *,
        target_person_id: str | None = None,
    ) -> bool:
        target_person_id = await self._require_authorized_person_target(
            actor, target, person_id=target_person_id
        )
        normalized = " ".join(content.split()).strip()
        if not normalized:
            raise ValueError("记忆内容不能为空")
        started = time.perf_counter()
        current = await self._memories.get_fact(memory_id)
        mutation = await self._apply_mutation(
            actor,
            target=ResolvedSubject(
                MemoryScopeType.PERSON,
                target,
                None,
                canonical_subject_person_id=target_person_id,
            ),
            operation=MemoryMutationOperation.CORRECT,
            fact_id=memory_id,
            new_content=normalized,
            memory_key=current.memory_key if current is not None else None,
            category=current.category if current is not None else None,
            kind=current.kind if current is not None else None,
            reason="deterministic_memory_update",
            confidence=1.0,
            importance=current.importance if current is not None else None,
        )
        if mutation is not None:
            updated = mutation.ok and mutation.new_fact_id is not None
            return updated

        async def write(session: AsyncSession) -> MemoryFact | None:
            await self._require_authorized_person_target(
                actor, target, person_id=target_person_id, session=session
            )
            before = await self._person_fact(
                session, memory_id, target=target, person_id=target_person_id
            )
            updated_row = await self._memories.update_explicit_person(
                memory_id,
                user_id=target,
                person_id=target_person_id,
                content=normalized,
                session=session,
            )
            updated = updated_row is not None
            await self._audit.record(
                actor=actor,
                capability="memory",
                operation="update",
                target_type="user",
                target_id=target,
                before={"memory_id": memory_id, "content": before.content if before else None},
                after={"memory_id": memory_id, "content": normalized},
                success=updated,
                error_category=None if updated else "not_found",
                duration_seconds=time.perf_counter() - started,
                session=session,
            )
            return updated_row

        prepared = await self._memories.prepare_write(
            (memory_id,), copies=(((memory_id,), current, None),) if current is not None else ()
        )
        async with self._memories.repository.write_session(prepared) as writer:
            updated_row = await write(writer)
        if updated_row is not None:
            await self._memories.schedule_embedding(updated_row.id)
        return updated_row is not None

    async def delete_memory(
        self,
        actor: AdminActor,
        target: str,
        memory_id: int,
        *,
        target_person_id: str | None = None,
    ) -> bool:
        target_person_id = await self._require_authorized_person_target(
            actor, target, person_id=target_person_id
        )
        started = time.perf_counter()
        mutation = await self._apply_mutation(
            actor,
            target=ResolvedSubject(
                MemoryScopeType.PERSON,
                target,
                None,
                canonical_subject_person_id=target_person_id,
            ),
            operation=MemoryMutationOperation.INVALIDATE,
            fact_id=memory_id,
            reason="deterministic_memory_delete",
        )
        if mutation is not None:
            deleted = mutation.ok
            return deleted

        async def write(session: AsyncSession) -> bool:
            await self._require_authorized_person_target(
                actor, target, person_id=target_person_id, session=session
            )
            before = await self._person_fact(
                session, memory_id, target=target, person_id=target_person_id
            )
            deleted = await self._memories.invalidate_person(
                memory_id,
                user_id=target,
                person_id=target_person_id,
                session=session,
            )
            await self._audit.record(
                actor=actor,
                capability="memory",
                operation="delete",
                target_type="user",
                target_id=target,
                before={"memory_id": memory_id, "content": before.content if before else None},
                after=None,
                success=deleted,
                error_category=None if deleted else "not_found",
                duration_seconds=time.perf_counter() - started,
                session=session,
            )
            return deleted

        prepared = await self._memories.prepare_write((memory_id,))
        async with self._memories.repository.write_session(prepared) as writer:
            return await write(writer)

    async def list_evidence(
        self,
        actor: AdminActor,
        target: str,
        memory_id: int,
        *,
        target_person_id: str | None = None,
    ) -> tuple[MemoryEvidence, ...]:
        async with self._database.read_session() as session:
            await session.execute(text("BEGIN"))
            target_person_id = await self._require_authorized_person_target(
                actor, target, person_id=target_person_id, session=session
            )
            fact = await self._memories.get_fact(memory_id, session=session)
            if (
                fact is None
                or fact.scope_type is not MemoryScopeType.PERSON
                or not await self._memories.person_owns_fact(
                    fact, actor_person_id=target_person_id, session=session
                )
            ):
                return ()
            return await self._memories.repository.list_evidence(memory_id, session=session)

    async def search_person(
        self,
        actor: AdminActor,
        user_id: str,
        query: str,
        *,
        limit: int = 20,
        person_id: str | None = None,
    ) -> MemoryRetrievalResult:
        person_id = await self._require_authorized_person_target(
            actor, user_id, person_id=person_id
        )
        runtime = await self._runtime_config.snapshot(user_id=person_id)
        target = MemoryEntityTarget(
            role=MemoryTargetRole.CURRENT_PERSON,
            scope_type=MemoryScopeType.PERSON,
            canonical_subject_person_id=person_id,
            block_id="admin_person",
        )
        result = await MemoryQueryPlane(self._memory_context).read(
            MemoryReadConsumer.ADMIN,
            MemoryReadRequest(
                text=query,
                resolved_scope=ResolvedReadScope(targets=(target,)),
                requested_limit=limit,
            ),
            runtime=runtime,
        )
        await self._require_authorized_person_target(actor, user_id, person_id=person_id)
        return result

    async def search_group(
        self,
        actor: AdminActor,
        group_id: str,
        query: str,
        *,
        limit: int = 20,
        space_id: str | None = None,
    ) -> MemoryRetrievalResult:
        if not await self._is_superuser(actor):
            raise PermissionError("只有超级管理员可以诊断群记忆")
        from yuki.admin.config_owners import resolve_space_owner

        async with self._database.read_session() as session:
            space_id = await resolve_space_owner(session, space_id or actor.space_id)
        runtime = await self._runtime_config.snapshot(group_id=space_id)
        target = MemoryEntityTarget(
            role=MemoryTargetRole.CURRENT_GROUP,
            scope_type=MemoryScopeType.GROUP,
            canonical_subject_space_id=space_id,
            block_id="admin_group",
        )
        result = await MemoryQueryPlane(self._memory_context).read(
            MemoryReadConsumer.ADMIN,
            MemoryReadRequest(
                text=query,
                resolved_scope=ResolvedReadScope(targets=(target,)),
                requested_limit=limit,
            ),
            runtime=runtime,
        )
        await self._require_superuser(actor)
        return result

    async def index_status(self, actor: AdminActor) -> MemoryIndexHealth:
        await self._require_superuser(actor)
        return await self._memory_index.health()

    async def rebuild_index(self, actor: AdminActor) -> MemoryIndexHealth:
        await self._require_superuser(actor)
        started = time.perf_counter()
        health = await self._memory_index.rebuild()
        await self._audit.record(
            actor=actor,
            capability="memory",
            operation="index_rebuild",
            target_type="derived_index",
            target_id="memory_facts_fts",
            before=None,
            after=health.model_dump(),
            success=True,
            duration_seconds=time.perf_counter() - started,
        )
        return health

    async def embedding_status(self, actor: AdminActor) -> MemoryEmbeddingHealth:
        await self._require_superuser(actor)
        if self._memory_embeddings is None:
            raise RuntimeError("memory embedding runtime is unavailable")
        return await self._memory_embeddings.health()

    async def embedding_doctor(self, actor: AdminActor) -> int:
        await self._require_superuser(actor)
        if self._memory_embeddings is None:
            raise RuntimeError("memory embedding runtime is unavailable")
        return await self._memory_embeddings.doctor()

    async def embedding_retry(self, actor: AdminActor) -> int:
        await self._require_superuser(actor)
        if self._memory_embeddings is None:
            raise RuntimeError("memory embedding runtime is unavailable")
        return await self._memory_embeddings.retry(
            actor_person_id=await self._actor_person_id(actor)
        )

    async def embedding_rebuild(self, actor: AdminActor) -> int:
        await self._require_superuser(actor)
        if self._memory_embeddings is None:
            raise RuntimeError("memory embedding runtime is unavailable")
        return await self._memory_embeddings.rebuild(
            actor_person_id=await self._actor_person_id(actor)
        )

    async def embedding_purge_old(self, actor: AdminActor) -> int:
        await self._require_superuser(actor)
        if self._memory_embeddings is None:
            raise RuntimeError("memory embedding runtime is unavailable")
        return await self._memory_embeddings.purge_old()

    async def show_fact(self, actor: AdminActor, fact_id: int) -> MemoryFact | None:
        async with self._database.read_session() as session:
            await session.execute(text("BEGIN"))
            fact = await self._fact_audit.get_fact(fact_id, session=session)
            await self._require_fact_access(actor, fact, session=session)
            return fact

    async def explain_fact(self, actor: AdminActor, fact_id: int) -> dict[str, object] | None:
        async with self._database.read_session() as session:
            await session.execute(text("BEGIN"))
            fact = await self._fact_audit.get_fact(fact_id, session=session)
            await self._require_fact_access(actor, fact, session=session)
            if fact is None:
                return None
            explanation = await self._fact_audit.explain(fact_id, session=session)
            if explanation is not None and await self._is_superuser(actor, session=session):
                explanation["evidence_sources"] = [
                    {
                        "event_id": row.event_id,
                        "source_speaker_user_id": row.source_speaker_user_id,
                        "relation": row.relation.value,
                        "excerpt": row.excerpt,
                    }
                    for row in await self._fact_audit.get_evidence(fact_id, session=session)
                ]
            return explanation

    async def fact_history(
        self, actor: AdminActor, fact_id: int
    ) -> tuple[MemoryFactStateEvent, ...]:
        async with self._database.read_session() as session:
            await session.execute(text("BEGIN"))
            fact = await self._fact_audit.get_fact(fact_id, session=session)
            await self._require_fact_access(actor, fact, session=session)
            return (
                await self._fact_audit.get_state_history(fact_id, session=session)
                if fact is not None
                else ()
            )

    async def list_conflicts(
        self,
        actor: AdminActor,
        *,
        target_user_id: str | None = None,
        person_id: str | None = None,
    ) -> tuple[MemoryFact, ...]:
        target = target_user_id or actor.user_id
        person_id = await self._require_authorized_person_target(actor, target, person_id=person_id)
        return await self._fact_audit.list_conflicts(person_id=person_id)

    async def correct_fact(
        self,
        actor: AdminActor | MemoryPreferenceTrigger,
        fact_id: int,
        content: str,
    ) -> MemoryFact | None:
        fact = await self._fact_audit.get_fact(fact_id)
        await self._require_authorized_fact_mutation(actor, fact)
        expected = normalize_memory_text(content)
        if not expected:
            raise ValueError("memory correction cannot be empty")
        result: MemoryFact | None = None
        if fact is not None:
            mutation = await self._apply_mutation(
                actor,
                target=ResolvedSubject(
                    fact.scope_type,
                    fact.subject_user_id,
                    fact.group_id,
                ),
                operation=MemoryMutationOperation.CORRECT,
                fact_id=fact_id,
                new_content=expected,
                memory_key=fact.memory_key,
                category=fact.category,
                kind=fact.kind,
                reason="memory_fact_correct_command",
                confidence=1.0,
                importance=fact.importance,
            )
            if mutation is not None:
                if mutation.ok and mutation.new_fact_id is not None:
                    result = await self._memories.get_fact(mutation.new_fact_id)
                else:
                    return None
            else:
                result = await self._memories.correct_fact(
                    fact_id,
                    content=expected,
                    actor_user_id=actor.user_id,
                )
        else:
            result = await self._memories.correct_fact(
                fact_id,
                content=expected,
                actor_user_id=actor.user_id,
            )
        return await self._visible_person_correction(
            result,
            expected_content=expected,
        )

    async def invalidate_fact(
        self,
        actor: AdminActor | MemoryPreferenceTrigger,
        fact_id: int,
        reason: str | None = None,
    ) -> bool:
        fact = await self._fact_audit.get_fact(fact_id)
        await self._require_authorized_fact_mutation(actor, fact)
        privileged = await self._is_superuser(actor)
        selected = (
            MemoryInvalidationReason.ADMINISTRATOR_INVALIDATED
            if privileged
            else MemoryInvalidationReason.USER_RETRACTED
        )
        if reason and privileged:
            selected = MemoryInvalidationReason(reason)
        if fact is not None:
            mutation = await self._apply_mutation(
                actor,
                target=ResolvedSubject(
                    fact.scope_type,
                    fact.subject_user_id,
                    fact.group_id,
                ),
                operation=MemoryMutationOperation.INVALIDATE,
                fact_id=fact_id,
                reason=selected.value,
            )
            if mutation is not None:
                return mutation.ok
        return await self._memories.invalidate_fact(
            fact_id,
            reason=selected,
            actor_user_id=actor.user_id,
        )

    async def restore_fact(self, actor: AdminActor, fact_id: int) -> MemoryFact | None:
        fact = await self._fact_audit.get_fact(fact_id)
        await self._require_fact_mutation(actor, fact)
        if (
            fact is not None
            and not await self._is_superuser(actor)
            and fact.invalidated_reason
            not in {
                MemoryInvalidationReason.USER_RETRACTED,
                MemoryInvalidationReason.PLUGIN_EXPLICIT_INVALIDATION,
            }
        ):
            raise PermissionError("只能恢复由本人撤回的记忆")
        if fact is not None:
            mutation = await self._apply_mutation(
                actor,
                target=ResolvedSubject(
                    fact.scope_type,
                    fact.subject_user_id,
                    fact.group_id,
                ),
                operation=MemoryMutationOperation.RESTORE,
                fact_id=fact_id,
                reason="memory_fact_restore_command",
            )
            if mutation is not None:
                return await self._mutation_fact(mutation, required=False)
        return await self._memories.restore_fact(fact_id, actor_user_id=actor.user_id)

    async def merge_facts(
        self,
        actor: AdminActor,
        source_fact_id: int,
        target_fact_id: int,
    ) -> MemoryFact | None:
        await self._require_superuser(actor)
        source = await self._fact_audit.get_fact(source_fact_id)
        if source is not None:
            mutation = await self._apply_mutation(
                actor,
                target=ResolvedSubject(
                    source.scope_type,
                    source.subject_user_id,
                    source.group_id,
                ),
                operation=MemoryMutationOperation.MERGE,
                fact_id=source_fact_id,
                merge_fact_id=target_fact_id,
                reason="memory_fact_merge_command",
            )
            if mutation is not None:
                return await self._mutation_fact(mutation, required=False)
        return await self._memories.merge_facts(
            source_fact_id,
            target_fact_id,
            actor_user_id=actor.user_id,
        )

    async def resolve_conflicts(
        self,
        actor: AdminActor,
        preferred_fact_id: int,
        contested_fact_ids: tuple[int, ...],
    ) -> int:
        await self._require_superuser(actor)
        return await self._memories.resolve_conflicts(
            preferred_fact_id,
            contested_fact_ids,
            actor_user_id=actor.user_id,
        )

    async def consistency_health(self, actor: AdminActor) -> MemoryConsistencyHealth:
        await self._require_superuser(actor)
        return await self._fact_audit.health()

    async def maintenance_status(
        self,
        actor: AdminActor,
    ) -> tuple[bool, MemoryConsistencyHealth]:
        await self._require_superuser(actor)
        health = await self._fact_audit.health()
        return bool(self._maintenance and self._maintenance.running), health

    async def run_maintenance_once(self) -> int:
        """Run one maintenance pass after capability authorization already happened."""

        if self._maintenance is None:
            raise RuntimeError("memory maintenance worker is unavailable")
        return await self._maintenance.process_once()

    async def maintenance_run(self, actor: AdminActor) -> int:
        await self._require_superuser(actor)
        return await self.run_maintenance_once()

    async def self_reflection_run(self, actor: AdminActor) -> dict[str, Any]:
        await self._require_superuser(actor)
        if self._self_reflection is None:
            raise RuntimeError("Self Reflection Worker 当前不可用")
        if actor.trigger_event_id is None or actor.canonical_conversation_id is None:
            raise ValueError("Self Reflection 需要内部事件与会话标识")
        return await self._self_reflection.run_now(
            source_event_id=actor.trigger_event_id, conversation_id=actor.canonical_conversation_id
        )

    async def self_reflection_status(
        self, actor: AdminActor, run_id: str | None
    ) -> dict[str, Any] | None:
        await self._require_superuser(actor)
        if self._self_reflection is None:
            raise RuntimeError("Self Reflection Worker 当前不可用")
        return await self._self_reflection.control.get(run_id)

    async def self_reflection_health(self, actor: AdminActor) -> dict[str, Any] | None:
        await self._require_superuser(actor)
        if self._self_reflection is None:
            return None
        return (await self._self_reflection.health()).model_dump(mode="json")

    async def self_reflection_retry(self, actor: AdminActor, run_id: int) -> bool:
        await self._require_superuser(actor)
        if self._self_reflection is None:
            raise RuntimeError("Self Reflection Worker 当前不可用")
        return await self._self_reflection.control.resume_batch(run_id)

    async def dream_plan(self, actor: AdminActor) -> DreamRun:
        await self._require_superuser(actor)
        if self._dream is None:
            raise RuntimeError("Memory Dream Worker 当前不可用")
        return await self._dream.plan_full(actor_user_id=actor.user_id)

    async def dream_start(self, actor: AdminActor, public_id: str) -> DreamRun:
        await self._require_superuser(actor)
        if self._dream is None:
            raise RuntimeError("Memory Dream Worker 当前不可用")
        return await self._dream.start_run(public_id)

    async def dream_list(self, actor: AdminActor) -> tuple[DreamRun, ...]:
        await self._require_superuser(actor)
        if self._dream is None:
            raise RuntimeError("Memory Dream Worker 当前不可用")
        return await self._dream.list_runs()

    async def dream_status(self, actor: AdminActor, public_id: str) -> DreamRun | None:
        await self._require_superuser(actor)
        if self._dream is None:
            raise RuntimeError("Memory Dream Worker 当前不可用")
        return await self._dream.status(public_id)

    async def dream_show(
        self,
        actor: AdminActor,
        public_id: str,
        *,
        page: int = 1,
    ) -> DreamRunPage:
        await self._require_superuser(actor)
        if self._dream is None:
            raise RuntimeError("Memory Dream 未初始化")
        return await self._dream.show(public_id, page=page)

    async def dream_preview(
        self,
        actor: AdminActor,
        public_id: str,
        *,
        cluster_id: int,
    ) -> DreamClusterPreview:
        await self._require_superuser(actor)
        if self._dream is None:
            raise RuntimeError("Memory Dream 未初始化")
        return await self._dream.preview(public_id, cluster_id=cluster_id)

    async def dream_cancel(self, actor: AdminActor, public_id: str) -> bool:
        await self._require_superuser(actor)
        if self._dream is None:
            raise RuntimeError("Memory Dream Worker 当前不可用")
        return await self._dream.cancel(public_id)

    async def dream_resume(self, actor: AdminActor, public_id: str) -> DreamRun:
        await self._require_superuser(actor)
        if self._dream is None:
            raise RuntimeError("Memory Dream Worker 当前不可用")
        return await self._dream.resume(public_id)

    async def dream_retry(self, actor: AdminActor, public_id: str) -> DreamRun:
        await self._require_superuser(actor)
        if self._dream is None:
            raise RuntimeError("Memory Dream Worker 当前不可用")
        return await self._dream.retry(public_id)

    async def dream_rollback_operation(self, actor: AdminActor, public_id: str) -> bool:
        await self._require_superuser(actor)
        if self._dream is None:
            raise RuntimeError("Memory Dream Worker 当前不可用")
        return await self._dream.rollback_operation(public_id)

    async def dream_rollback_run(self, actor: AdminActor, public_id: str) -> int:
        await self._require_superuser(actor)
        if self._dream is None:
            raise RuntimeError("Memory Dream Worker 当前不可用")
        return await self._dream.rollback_run(public_id)

    async def _apply_mutation(
        self,
        actor: AdminActor | MemoryPreferenceTrigger,
        *,
        target: ResolvedSubject,
        operation: MemoryMutationOperation,
        reason: str,
        fact_id: int | None = None,
        merge_fact_id: int | None = None,
        new_content: str | None = None,
        memory_key: str | None = None,
        category: str | None = None,
        kind: MemoryKind | None = None,
        confidence: float = 1.0,
        importance: int | None = None,
    ) -> MemoryMutationResult | None:
        if self._mutations is None and self._ledger is None:
            return None
        if self._mutations is None or self._ledger is None:
            raise RuntimeError("memory mutation dependencies are incomplete")
        if not actor.bot_user_id:
            raise RuntimeError("memory mutation is not bound to a real Bot event")
        if actor.trigger_event_id is None:
            raise RuntimeError("memory mutation requires a ledger event anchor")
        event = await self._ledger.get_event(actor.trigger_event_id)
        if (
            event is None
            or event.sender_user_id != actor.user_id
            or event.bot_user_id != actor.bot_user_id
            or event.direction != "inbound"
            or event.event_kind != "message"
            or event.suppression_status not in {None, "keeper"}
            or (
                actor.canonical_conversation_id is not None
                and event.canonical_conversation_id != actor.canonical_conversation_id
            )
            or (
                actor.ingress_presence_id is not None
                and event.ingress_presence_id != actor.ingress_presence_id
            )
        ):
            raise RuntimeError("memory mutation trigger event cannot be verified")
        try:
            decision_actor_type = MemoryDecisionActorType(actor.decision_actor_type)
        except ValueError:
            decision_actor_type = MemoryDecisionActorType.ADMIN
        quote_source = event.evidence_content
        preferred_quote = " ".join((new_content or "").split()).strip()
        evidence_quote = (
            preferred_quote
            if preferred_quote and len(preferred_quote) <= 500 and preferred_quote in quote_source
            else quote_source[:500]
        )
        effective_key = memory_key
        if operation is MemoryMutationOperation.CREATE and not effective_key:
            effective_key = f"explicit:{event.id}"
        request = MemoryMutationRequest(
            operation=operation,
            fact_id=fact_id,
            merge_fact_id=merge_fact_id,
            target=MemoryMutationTarget(
                subject_ref="current_speaker",
                scope_type=target.scope_type,
            ),
            new_content=new_content,
            memory_key=effective_key,
            category=category,
            kind=kind,
            reason=reason,
            confidence=confidence,
            importance=importance,
            evidence_quote=evidence_quote,
        )
        if fact_id is not None and operation is not MemoryMutationOperation.REASSIGN:
            original_fact = await self._memories.get_fact(fact_id)
            if original_fact is not None:
                target = ResolvedSubject(
                    original_fact.scope_type,
                    original_fact.subject_user_id,
                    original_fact.group_id,
                    original_fact.visibility_type,
                    original_fact.visibility_user_id,
                    original_fact.visibility_group_id,
                    original_fact.canonical_subject_person_id,
                    original_fact.canonical_subject_space_id,
                    original_fact.canonical_visibility_person_id,
                    original_fact.canonical_visibility_space_id,
                )
        return await self._mutations.mutate_resolved(
            request,
            MemoryMutationContext(
                event=event,
                conversation_key=actor.conversation_key,
                turn_origin=event.origin,
                delegation_mode=decision_actor_type.value,
                trigger_actor_user_id=event.sender_user_id,
                decision_actor_type=decision_actor_type,
                decision_actor_id=actor.decision_actor_id or actor.user_id,
                executed_by_bot_user_id=event.bot_user_id,
                actor_is_superuser=await self._is_superuser(actor),
                actor_person_id=event.author_person_id,
                operation_id=f"memory-admin:event:{event.id}",
            ),
            target=target,
        )

    async def _visible_person_correction(
        self,
        fact: MemoryFact | None,
        *,
        expected_content: str,
    ) -> MemoryFact | None:
        if (
            fact is None
            or fact.content != expected_content
            or fact.scope_type is not MemoryScopeType.PERSON
            or fact.canonical_subject_person_id is None
        ):
            return None
        projection = await self._memories.list_person(
            person_id=fact.canonical_subject_person_id,
        )
        return next((row for row in projection if row.id == fact.id), None)

    async def _mutation_fact(
        self,
        mutation: MemoryMutationResult,
        *,
        required: bool = True,
    ) -> MemoryFact | None:
        fact_id = mutation.new_fact_id or mutation.old_fact_id
        row = await self._memories.get_fact(fact_id) if fact_id is not None else None
        if row is None and required:
            raise ValueError(f"记忆变更未提交：{mutation.reason_code}")
        return row

    async def _person_fact(
        self, session: AsyncSession, fact_id: int, *, target: str, person_id: str
    ) -> MemoryFact | None:
        fact = await self._memories.get_fact(fact_id, session=session)
        if (
            fact is not None
            and fact.scope_type is MemoryScopeType.PERSON
            and fact.status is MemoryStatus.ACTIVE
            and await self._memories.person_owns_fact(
                fact, actor_user_id=target, actor_person_id=person_id, session=session
            )
        ):
            return fact
        return None

    async def _require_authorized_person_target(
        self,
        actor: AdminActor | MemoryPreferenceTrigger,
        target: str,
        *,
        person_id: str | None = None,
        session: AsyncSession | None = None,
    ) -> str:
        from yuki.identity.errors import CanonicalIdentityError
        from yuki.identity.person_identity import (
            current_person_id,
            person_enabled_condition,
            same_person_id,
        )
        from yuki.persistence.unit_of_work import optional_session

        async with optional_session(self._database, session, write=False) as reader:
            actor_person_id = await self._actor_person_id(actor, session=reader)
            if actor_person_id is not None and not await reader.scalar(
                select(person_enabled_condition(actor_person_id))
            ):
                raise CanonicalIdentityError("canonical_owner_disabled")
            subject = person_id or (actor_person_id if target == actor.user_id else None)
            if subject is None:
                raise CanonicalIdentityError("missing_canonical_owner")
            target_person_id = await current_person_id(reader, subject)
            own = actor_person_id is not None and await same_person_id(
                reader, actor_person_id, target_person_id
            )
            privileged = await self._is_superuser(actor, session=reader)
        if privileged or own:
            return target_person_id
        raise PermissionError("只有当前真实超级管理员可以执行该操作")

    async def _require_authorized_fact_mutation(
        self,
        actor: AdminActor | MemoryPreferenceTrigger,
        fact: MemoryFact | None,
    ) -> None:
        if fact is None:
            return
        if await self._is_superuser(actor):
            return
        if not await self._memories.person_owns_fact(
            fact,
            actor_user_id=actor.user_id,
            actor_person_id=await self._actor_person_id(actor),
        ):
            raise PermissionError("只能查看与本人有关的人物记忆")
        if fact.scope_type is MemoryScopeType.GROUP:
            raise PermissionError("普通用户不能修改群共同事实")

    async def _require_fact_access(
        self, actor: AdminActor, fact: MemoryFact | None, *, session: AsyncSession | None = None
    ) -> None:
        if fact is None:
            return
        if await self._is_superuser(actor, session=session):
            return
        if not await self._memories.person_owns_fact(
            fact,
            actor_user_id=actor.user_id,
            actor_person_id=await self._actor_person_id(actor, session=session),
            session=session,
        ):
            raise PermissionError("只能查看与本人有关的人物记忆")

    async def _require_fact_mutation(
        self,
        actor: AdminActor,
        fact: MemoryFact | None,
    ) -> None:
        await self._require_fact_access(actor, fact)
        if (
            fact is not None
            and fact.scope_type is MemoryScopeType.GROUP
            and not await self._is_superuser(actor)
        ):
            raise PermissionError("普通用户不能修改群共同事实")

    async def _require_superuser(self, actor: AdminActor) -> None:
        if not await self._is_superuser(actor):
            raise PermissionError("只有超级管理员可以执行此记忆管理操作")

    async def _is_superuser(
        self, actor: AdminActor | MemoryPreferenceTrigger, *, session: AsyncSession | None = None
    ) -> bool:
        from yuki.identity.authority import person_is_superuser
        from yuki.persistence.unit_of_work import optional_session

        async with optional_session(self._database, session, write=False) as reader:
            person_id = await self._actor_person_id(actor, session=reader)
            return await person_is_superuser(reader, person_id)

    async def _actor_person_id(
        self,
        actor: AdminActor | MemoryPreferenceTrigger,
        *,
        session: AsyncSession | None = None,
    ) -> str | None:
        if actor.person_id is not None:
            return actor.person_id
        if self._ledger is not None and actor.trigger_event_id is not None:
            from yuki.persistence.models import ChatEventModel
            from yuki.persistence.unit_of_work import optional_session

            async with optional_session(self._database, session, write=False) as reader:
                event = await reader.get(ChatEventModel, actor.trigger_event_id)
            if event is not None and event.sender_user_id == actor.user_id:
                return event.author_person_id
            return None
        return None
