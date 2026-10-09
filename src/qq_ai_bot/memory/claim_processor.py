"""Shared validation, conflict resolution, and fact persistence pipeline."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from qq_ai_bot.config import Settings
from qq_ai_bot.domain.memory_config import MemoryConfigScope
from qq_ai_bot.memory.enums import (
    MemoryConflictState,
    MemoryInvalidationReason,
    MemoryProcessingSource,
    MemoryResolutionAction,
    MemoryStatus,
)
from qq_ai_bot.memory.extraction import MemoryClaim
from qq_ai_bot.memory.metrics import MemoryLifecycleMetrics
from qq_ai_bot.memory.models import MemoryCandidate, MemoryResolutionPlan
from qq_ai_bot.memory.service import MemoryFactService
from qq_ai_bot.memory.subjects import SubjectResolutionContext
from qq_ai_bot.memory.validation import (
    MemoryClaimValidationResult,
    MemoryClaimValidator,
    ValidatedMemoryClaim,
)
from qq_ai_bot.persistence.repository_records import EventRecord


@dataclass(frozen=True, slots=True)
class MemoryProcessingContext:
    source: MemoryProcessingSource
    event: EventRecord | None
    config_scope: MemoryConfigScope | None = None
    rebuild_run_id: str | None = None
    proposal_id: int | None = None
    force_expired_invalidated: bool = False


@dataclass(frozen=True, slots=True)
class MemoryClaimProcessResult:
    fact_id: int | None
    action: MemoryResolutionAction
    reason_code: str


@dataclass(frozen=True, slots=True)
class ResolvedMemoryClaim:
    """A fully decided claim that can be persisted without calling a model."""

    claim: ValidatedMemoryClaim
    candidates: tuple[MemoryCandidate, ...]
    plan: MemoryResolutionPlan
    action: MemoryResolutionAction
    reason_code: str


MemoryClaimResolution = ResolvedMemoryClaim | MemoryClaimProcessResult


class MemoryClaimProcessor:
    """The only automatic route from a validated claim to MemoryFactService."""

    def __init__(
        self,
        *,
        settings: Settings,
        facts: MemoryFactService,
        validator: MemoryClaimValidator | None = None,
        metrics: MemoryLifecycleMetrics | None = None,
    ) -> None:
        self._settings = settings
        self._facts = facts
        self._validator = validator or MemoryClaimValidator(
            timezone_name=settings.default_timezone,
        )
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

        if context.event is None and (
            not isinstance(claim, ValidatedMemoryClaim) or context.config_scope is None
        ):
            raise ValueError(
                "actorless processing requires validated claim and canonical config scope"
            )
        if isinstance(claim, ValidatedMemoryClaim):
            validated: ValidatedMemoryClaim | None = claim
        else:
            assert context.event is not None  # Actorless inputs were rejected above.
            validated = self._validator.validate_claim(claim, context.event)
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
                action=MemoryResolutionAction.INVALIDATE,
                reason_code="historical_expired",
            )
        return ResolvedMemoryClaim(
            claim=validated,
            candidates=(),
            plan=MemoryResolutionPlan(
                action=MemoryResolutionAction.CREATE,
                reason_code="independent_claim",
                append_evidence=True,
                create_new_fact=True,
            ),
            action=MemoryResolutionAction.CREATE,
            reason_code="independent_claim",
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
            session=session,
        )
        return MemoryClaimProcessResult(
            fact.id if fact is not None else None,
            resolution.action,
            resolution.reason_code,
        )
