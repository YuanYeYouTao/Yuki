"""Shared validation, conflict resolution, and fact persistence pipeline."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC

from sqlalchemy.ext.asyncio import AsyncSession

from qq_ai_bot.admin.config_service import RuntimeConfigService
from qq_ai_bot.config import Settings
from qq_ai_bot.domain.memory_config import MemoryConfigScope
from qq_ai_bot.llm.base import LLMError
from qq_ai_bot.memory.candidates import MemoryConflictCandidateResolver
from qq_ai_bot.memory.classifier import MemoryRelationClassifier
from qq_ai_bot.memory.enums import (
    MemoryClaimOperation,
    MemoryConflictState,
    MemoryFactRelationType,
    MemoryInvalidationReason,
    MemoryProcessingSource,
    MemoryResolutionAction,
    MemoryScopeType,
    MemoryStatus,
    SelfMemoryVisibility,
)
from qq_ai_bot.memory.extraction import MemoryClaim
from qq_ai_bot.memory.metrics import MemoryLifecycleMetrics
from qq_ai_bot.memory.models import CandidateRelation, MemoryCandidate, MemoryResolutionPlan
from qq_ai_bot.memory.resolution import MemoryResolutionPolicy
from qq_ai_bot.memory.service import MemoryFactService
from qq_ai_bot.memory.subjects import SubjectResolutionContext
from qq_ai_bot.memory.validation import (
    MemoryClaimValidationResult,
    MemoryClaimValidator,
    ValidatedMemoryClaim,
)
from qq_ai_bot.model_runtime.structured import StructuredTaskError
from qq_ai_bot.persistence.repository_records import EventRecord


@dataclass(frozen=True, slots=True)
class MemoryProcessingContext:
    source: MemoryProcessingSource
    event: EventRecord
    config_scope: MemoryConfigScope | None = None
    rebuild_run_id: str | None = None
    proposal_id: int | None = None
    preserve_capacity: bool = False
    force_expired_invalidated: bool = False


@dataclass(frozen=True, slots=True)
class MemoryClaimProcessResult:
    fact_id: int | None
    action: MemoryResolutionAction
    reason_code: str
    model_requests: int = 0
    input_tokens: int | None = None
    output_tokens: int | None = None
    latency_seconds: float = 0.0


@dataclass(frozen=True, slots=True)
class ResolvedMemoryClaim:
    """A fully decided claim that can be persisted without calling a model."""

    claim: ValidatedMemoryClaim
    candidates: tuple[MemoryCandidate, ...]
    plan: MemoryResolutionPlan
    limit: int | None
    action: MemoryResolutionAction
    reason_code: str
    model_requests: int = 0
    input_tokens: int | None = None
    output_tokens: int | None = None
    latency_seconds: float = 0.0


MemoryClaimResolution = ResolvedMemoryClaim | MemoryClaimProcessResult


class MemoryHistoricalResolutionGuard:
    """Prevent old evidence from reversing newer current state."""

    @staticmethod
    def protect(
        claim: ValidatedMemoryClaim,
        candidates: tuple[MemoryCandidate, ...],
        plan: MemoryResolutionPlan,
    ) -> MemoryResolutionPlan:
        if plan.existing_fact_id is None:
            return plan
        existing = next(
            (row.fact for row in candidates if row.fact.id == plan.existing_fact_id),
            None,
        )
        claim_time = (
            claim.occurred_at.replace(tzinfo=UTC)
            if claim.occurred_at.tzinfo is None
            else claim.occurred_at.astimezone(UTC)
        )
        confirmed_at = (
            existing.last_confirmed_at.replace(tzinfo=UTC)
            if existing is not None and existing.last_confirmed_at.tzinfo is None
            else existing.last_confirmed_at.astimezone(UTC)
            if existing is not None
            else None
        )
        if existing is None or confirmed_at is None or claim_time >= confirmed_at:
            return plan
        if plan.action is MemoryResolutionAction.MERGE_EVIDENCE:
            return plan
        if plan.action in {
            MemoryResolutionAction.SUPERSEDE,
            MemoryResolutionAction.CONTEST,
        }:
            return MemoryResolutionPlan(
                action=MemoryResolutionAction.CREATE,
                existing_fact_id=existing.id,
                new_fact_status=MemoryStatus.SUPERSEDED,
                new_conflict_state=MemoryConflictState.CLEAR,
                relation_types=(
                    plan.relation_types
                    if plan.relation_types
                    else (MemoryFactRelationType.CONTRADICTS,)
                ),
                reason_code="historical_version_preserved",
                append_evidence=True,
                create_new_fact=True,
            )
        return MemoryResolutionPlan(
            action=MemoryResolutionAction.NOOP,
            existing_fact_id=existing.id,
            reason_code="historical_newer_fact_preserved",
            append_evidence=False,
            create_new_fact=False,
        )


class MemoryClaimProcessor:
    """The only automatic route from a validated claim to MemoryFactService."""

    def __init__(
        self,
        *,
        settings: Settings,
        facts: MemoryFactService,
        candidate_resolver: MemoryConflictCandidateResolver,
        relation_classifier: MemoryRelationClassifier,
        resolution_policy: MemoryResolutionPolicy,
        validator: MemoryClaimValidator | None = None,
        runtime_config: RuntimeConfigService | None = None,
        metrics: MemoryLifecycleMetrics | None = None,
    ) -> None:
        self._settings = settings
        self._facts = facts
        self._candidates = candidate_resolver
        self._classifier = relation_classifier
        self._resolution = resolution_policy
        self._validator = validator or MemoryClaimValidator(
            timezone_name=settings.default_timezone,
            bot_aliases=settings.bot_aliases,
        )
        self._runtime_config = runtime_config
        self.metrics = metrics or MemoryLifecycleMetrics()

    def validate(
        self,
        claim: MemoryClaim,
        event: EventRecord,
        *,
        subject_context: SubjectResolutionContext | None = None,
    ) -> ValidatedMemoryClaim | None:
        return self._validator.validate_claim(
            claim,
            event,
            subject_context=subject_context,
        )

    def validate_result(
        self,
        claim: MemoryClaim,
        event: EventRecord,
        *,
        subject_context: SubjectResolutionContext | None = None,
    ) -> MemoryClaimValidationResult:
        return self._validator.validate_claim_result(
            claim,
            event,
            subject_context=subject_context,
        )

    async def process(
        self,
        claim: MemoryClaim | ValidatedMemoryClaim,
        context: MemoryProcessingContext,
        *,
        session: AsyncSession | None = None,
    ) -> MemoryClaimProcessResult:
        resolution = await self.resolve(claim, context)
        return await self.apply_resolution(resolution, session=session)

    async def resolve(
        self,
        claim: MemoryClaim | ValidatedMemoryClaim,
        context: MemoryProcessingContext,
    ) -> MemoryClaimResolution:
        """Validate and decide a claim without opening a write transaction."""

        validated = (
            claim
            if isinstance(claim, ValidatedMemoryClaim)
            else self._validator.validate_claim(claim, context.event)
        )
        if validated is None:
            return MemoryClaimProcessResult(None, MemoryResolutionAction.NOOP, "claim_rejected")
        if context.force_expired_invalidated:
            expired = validated.fact.model_copy(
                update={
                    "status": MemoryStatus.INVALIDATED,
                    "conflict_state": MemoryConflictState.CLEAR,
                    "invalidated_reason": MemoryInvalidationReason.EXPIRED,
                }
            )
            historical = ValidatedMemoryClaim(
                operation=validated.operation,
                fact=expired,
                evidence=validated.evidence,
                subject_is_speaker=validated.subject_is_speaker,
                occurred_at=validated.occurred_at,
                subject_basis=validated.subject_basis,
                retention=validated.retention,
                source_style=validated.source_style,
            )
            return ResolvedMemoryClaim(
                claim=historical,
                candidates=(),
                plan=MemoryResolutionPlan(
                    action=MemoryResolutionAction.CREATE,
                    new_fact_status=MemoryStatus.INVALIDATED,
                    new_conflict_state=MemoryConflictState.CLEAR,
                    reason_code="historical_expired",
                    append_evidence=True,
                    create_new_fact=True,
                ),
                limit=None,
                action=MemoryResolutionAction.INVALIDATE,
                reason_code="historical_expired",
            )
        runtime = (
            await self._runtime_config.snapshot(memory_scope=context.config_scope)
            if self._runtime_config is not None and context.config_scope is not None
            else await self._runtime_config.snapshot(
                user_id=context.event.sender_user_id,
                group_id=context.event.group_id,
            )
            if self._runtime_config is not None
            else None
        )
        candidates = await self._candidates.resolve(
            validated.fact,
            limit=(
                runtime.memory.consolidation_candidate_limit
                if runtime is not None
                else self._settings.memory_consolidation_candidate_limit
            ),
        )
        threshold = (
            runtime.memory.consolidation_min_relevance
            if runtime is not None
            else self._settings.memory_consolidation_min_relevance
        )
        candidates = tuple(
            row
            for row in candidates
            if row.exact_key or row.exact_content or row.relevance >= threshold
        )
        if self._covered_by_global_self_fact(validated, candidates):
            self.metrics.increment("global_self_coverage_noops")
            return MemoryClaimProcessResult(
                None,
                MemoryResolutionAction.NOOP,
                "global_already_covers",
            )
        relations: tuple[CandidateRelation, ...] = ()
        model_requests = 0
        input_tokens: int | None = None
        output_tokens: int | None = None
        latency_seconds = 0.0
        consolidation = (
            runtime.memory.consolidation_enabled
            if runtime is not None
            else self._settings.memory_consolidation_enabled
        )
        if (
            consolidation
            and candidates
            and not self._is_deterministic(validated.operation, candidates)
        ):
            self.metrics.increment("classifier_requests")
            try:
                classified = await self._classifier.classify_with_usage(
                    validated,
                    candidates,
                    max_output_tokens=(
                        runtime.memory.consolidation_max_output_tokens
                        if runtime is not None
                        else self._settings.memory_consolidation_max_output_tokens
                    ),
                )
                relations = classified.classification.relations
                model_requests = 1
                input_tokens = classified.input_tokens
                output_tokens = classified.output_tokens
                latency_seconds = classified.latency_seconds
            except asyncio.CancelledError:
                raise
            except (LLMError, StructuredTaskError, OSError, RuntimeError, TypeError, ValueError):
                self.metrics.increment("classifier_failures")
                self.metrics.record_classifier_error()
        else:
            self.metrics.increment("deterministic_resolutions")
        plan = self._resolution.resolve(validated, candidates, relations)
        if context.source is MemoryProcessingSource.REBUILD:
            plan = MemoryHistoricalResolutionGuard.protect(validated, candidates, plan)
        limit = self._scope_limit(validated.fact.scope_type)
        creates_current_fact = plan.new_fact_status in {
            None,
            MemoryStatus.ACTIVE,
            MemoryStatus.CONTESTED,
        }
        if context.preserve_capacity and plan.create_new_fact and creates_current_fact:
            query = validated.fact
            active = await self._facts.repository.count_active_for_create(query)
            if active >= limit:
                return MemoryClaimProcessResult(
                    None,
                    MemoryResolutionAction.NOOP,
                    "rebuild_capacity_preserved",
                )
        return ResolvedMemoryClaim(
            claim=validated,
            candidates=candidates,
            plan=plan,
            limit=None if context.preserve_capacity else limit,
            action=plan.action,
            reason_code=plan.reason_code,
            model_requests=model_requests,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            latency_seconds=latency_seconds,
        )

    @staticmethod
    def _covered_by_global_self_fact(
        claim: ValidatedMemoryClaim,
        candidates: tuple[MemoryCandidate, ...],
    ) -> bool:
        fact = claim.fact
        if (
            fact.scope_type is not MemoryScopeType.SELF
            or fact.visibility_type is SelfMemoryVisibility.GLOBAL
        ):
            return False
        return any(
            candidate.exact_key
            and candidate.exact_content
            and candidate.fact.scope_type is MemoryScopeType.SELF
            and candidate.fact.visibility_type is SelfMemoryVisibility.GLOBAL
            for candidate in candidates
        )

    async def apply_resolution(
        self,
        resolution: MemoryClaimResolution,
        *,
        session: AsyncSession | None = None,
    ) -> MemoryClaimProcessResult:
        """Persist a previously decided claim without performing model I/O."""

        if isinstance(resolution, MemoryClaimProcessResult):
            return resolution
        fact = await self._facts.apply_claim(
            resolution.claim,
            candidates=resolution.candidates,
            plan=resolution.plan,
            limit=resolution.limit,
            session=session,
        )
        return MemoryClaimProcessResult(
            fact.id if fact is not None else None,
            resolution.action,
            resolution.reason_code,
            model_requests=resolution.model_requests,
            input_tokens=resolution.input_tokens,
            output_tokens=resolution.output_tokens,
            latency_seconds=resolution.latency_seconds,
        )

    @staticmethod
    def _is_deterministic(
        operation: MemoryClaimOperation,
        candidates: tuple[MemoryCandidate, ...],
    ) -> bool:
        if not candidates or any(candidate.exact_content for candidate in candidates):
            return True
        exact = tuple(candidate for candidate in candidates if candidate.exact_key)
        return len(exact) == 1 and operation in {
            MemoryClaimOperation.CORRECT,
            MemoryClaimOperation.RETRACT,
        }

    def _scope_limit(self, scope: MemoryScopeType) -> int:
        if scope in {MemoryScopeType.PERSON, MemoryScopeType.SELF}:
            return self._settings.person_memory_max_entries
        if scope is MemoryScopeType.GROUP:
            return self._settings.group_memory_max_entries
        return self._settings.person_group_memory_max_entries
