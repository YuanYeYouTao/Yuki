"""Backend validation that prevents model-selected identity attribution."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from qq_ai_bot.memory.enums import (
    MemoryAuthority,
    MemoryClaimOperation,
    MemoryEvidenceRelation,
    MemoryKind,
    MemoryRetention,
    MemoryScopeType,
    MemorySourceType,
)
from qq_ai_bot.memory.extraction import MemoryClaim
from qq_ai_bot.memory.models import MemoryEvidenceCreate, MemoryFactCreate
from qq_ai_bot.memory.quality_policy import (
    AttributionPolicy,
    RetentionPolicy,
)
from qq_ai_bot.memory.subjects import SubjectResolutionContext, SubjectResolver
from qq_ai_bot.memory.temporal import MemoryTemporalResolver
from qq_ai_bot.persistence.repository_records import EventRecord

_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")


@dataclass(frozen=True, slots=True)
class ValidatedMemoryClaim:
    operation: MemoryClaimOperation
    fact: MemoryFactCreate
    evidence: MemoryEvidenceCreate
    subject_is_speaker: bool
    occurred_at: datetime
    subject_basis: str = ""
    retention: str = ""
    source_style: str = ""


@dataclass(frozen=True, slots=True)
class MemoryClaimValidationResult:
    """A content-free explanation of one claim validation decision."""

    claim: ValidatedMemoryClaim | None
    reason_code: str
    candidate_type: str | None = None
    raw_claim: MemoryClaim | None = None

    @property
    def ok(self) -> bool:
        return self.claim is not None


class _MemoryClaimRejected(ValueError):
    """Internal control flow carrying a stable, content-free reason code."""

    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


class _MemoryClaimCandidate(_MemoryClaimRejected):
    def __init__(self, reason_code: str, candidate_type: str) -> None:
        super().__init__(reason_code)
        self.candidate_type = candidate_type


def normalize_memory_text(value: str, *, maximum: int) -> str:
    """Flatten untrusted model/user text before persistence and prompt use."""

    return " ".join(_CONTROL.sub(" ", value).split())[:maximum].strip()


class MemoryClaimValidator:
    """Turn a claim into trusted persistence input or reject it without guessing."""

    def __init__(
        self,
        resolver: SubjectResolver | None = None,
        temporal: MemoryTemporalResolver | None = None,
        *,
        timezone_name: str = "Asia/Shanghai",
        bot_aliases: tuple[str, ...] | None = None,
    ) -> None:
        del bot_aliases
        self._resolver = resolver or SubjectResolver()
        self._temporal = temporal or MemoryTemporalResolver()
        self._timezone_name = timezone_name

    def validate(
        self,
        claim: MemoryClaim,
        event: EventRecord,
        *,
        subject_context: SubjectResolutionContext | None = None,
    ) -> tuple[MemoryFactCreate, MemoryEvidenceCreate] | None:
        validated = self.validate_claim(claim, event, subject_context=subject_context)
        if validated is None:
            return None
        return validated.fact, validated.evidence

    def validate_claim(
        self,
        claim: MemoryClaim,
        event: EventRecord,
        *,
        subject_context: SubjectResolutionContext | None = None,
    ) -> ValidatedMemoryClaim | None:
        return self.validate_claim_result(
            claim,
            event,
            subject_context=subject_context,
        ).claim

    def validate_claim_result(
        self,
        claim: MemoryClaim,
        event: EventRecord,
        *,
        subject_context: SubjectResolutionContext | None = None,
    ) -> MemoryClaimValidationResult:
        """Validate one claim while preserving why a rejected claim was dropped."""

        try:
            validated = self._validate_claim(
                claim,
                event,
                subject_context=subject_context,
            )
        except _MemoryClaimCandidate as exc:
            return MemoryClaimValidationResult(
                None,
                exc.reason_code,
                candidate_type=exc.candidate_type,
                raw_claim=claim,
            )
        except _MemoryClaimRejected as exc:
            return MemoryClaimValidationResult(None, exc.reason_code, raw_claim=claim)
        return MemoryClaimValidationResult(validated, "validated", raw_claim=claim)

    def _validate_claim(
        self,
        claim: MemoryClaim,
        event: EventRecord,
        *,
        subject_context: SubjectResolutionContext | None = None,
    ) -> ValidatedMemoryClaim:
        if not event.evidence_content.strip():
            raise _MemoryClaimRejected("empty_event")
        raw_quote = claim.evidence_quote.strip()
        if not raw_quote or raw_quote not in event.evidence_content:
            raise _MemoryClaimRejected("evidence_quote_not_in_event")
        quote = normalize_memory_text(raw_quote, maximum=500)
        source = normalize_memory_text(event.evidence_content, maximum=4000)
        if not quote or quote not in source:
            raise _MemoryClaimRejected("normalized_evidence_not_in_event")
        resolved = self._resolver.resolve(
            event,
            subject_ref=claim.subject_ref,
            scope_type=claim.scope_type,
            context=subject_context,
        )
        attribution = AttributionPolicy.evaluate(
            claim,
            event,
            resolved,
        )
        if attribution.candidate_type is not None:
            raise _MemoryClaimCandidate(attribution.reason_code, attribution.candidate_type)
        if not attribution.accepted:
            raise _MemoryClaimRejected(attribution.reason_code)
        explicit_request = claim.source_type is MemorySourceType.EXPLICIT
        retention = RetentionPolicy.evaluate(
            claim,
            event,
            explicit_request=explicit_request,
        )
        if not retention.accepted:
            raise _MemoryClaimRejected(retention.reason_code)
        if claim.confidence < 0.65 and not explicit_request:
            raise _MemoryClaimCandidate("low_confidence_candidate", "memory")
        if resolved is None:
            raise _MemoryClaimRejected("subject_unresolved")
        key = normalize_memory_text(claim.memory_key, maximum=128)
        category = normalize_memory_text(claim.category, maximum=64)
        content = normalize_memory_text(claim.content, maximum=4000)
        if not key or not category or not content:
            raise _MemoryClaimRejected("incomplete_claim_fields")
        kind = claim.kind
        if claim.retention is MemoryRetention.MEANINGFUL_EPISODE:
            kind = MemoryKind.EPISODE
        subject_is_speaker = resolved.subject_user_id == event.sender_user_id
        is_third_party = bool(resolved.subject_user_id) and not subject_is_speaker
        if is_third_party and resolved.scope_type is not MemoryScopeType.PERSON_GROUP:
            raise _MemoryClaimRejected("third_party_scope_not_person_group")
        source_type = claim.source_type
        if is_third_party:
            source_type = MemorySourceType.AUTOMATIC
            authority = MemoryAuthority.THIRD_PARTY
        elif source_type is MemorySourceType.EXPLICIT:
            authority = MemoryAuthority.EXPLICIT
        elif resolved.scope_type is MemoryScopeType.GROUP:
            authority = MemoryAuthority.GROUP_REPORT
        else:
            authority = MemoryAuthority.SELF_REPORT
        try:
            temporal = self._temporal.resolve(
                mode=claim.temporal_mode,
                valid_from=claim.valid_from,
                valid_until=claim.valid_until,
                occurred_at=event.occurred_at,
                timezone_name=self._timezone_name,
            )
        except ValueError:
            raise _MemoryClaimRejected("invalid_temporal_value") from None
        fact = MemoryFactCreate(
            scope_type=resolved.scope_type,
            subject_user_id=resolved.subject_user_id,
            group_id=resolved.group_id,
            kind=kind,
            memory_key=key,
            category=category,
            content=content,
            importance=claim.importance,
            confidence=claim.confidence,
            source_type=source_type,
            authority=authority,
            valid_from=temporal.valid_from,
            valid_until=temporal.valid_until,
        )
        relation = self._evidence_relation(
            operation=claim.operation,
            authority=authority,
            source_type=source_type,
        )
        evidence = MemoryEvidenceCreate(
            event_id=event.id,
            source_speaker_user_id=event.sender_user_id,
            relation=relation,
            confidence=claim.confidence,
            authority=authority,
            excerpt=quote,
        )
        return ValidatedMemoryClaim(
            operation=claim.operation,
            fact=fact,
            evidence=evidence,
            subject_is_speaker=subject_is_speaker,
            occurred_at=event.occurred_at,
            subject_basis=claim.subject_basis.value,
            retention=claim.retention.value,
            source_style=claim.source_style.value,
        )

    @staticmethod
    def _evidence_relation(
        *,
        operation: MemoryClaimOperation,
        authority: MemoryAuthority,
        source_type: MemorySourceType,
    ) -> MemoryEvidenceRelation:
        if operation is MemoryClaimOperation.RETRACT:
            return MemoryEvidenceRelation.RETRACTION
        if operation is MemoryClaimOperation.CORRECT:
            return MemoryEvidenceRelation.CORRECTION
        if operation is MemoryClaimOperation.CONFIRM:
            return MemoryEvidenceRelation.CONFIRMATION
        if source_type is MemorySourceType.REBUILD:
            return MemoryEvidenceRelation.REBUILD
        if source_type is MemorySourceType.EXPLICIT:
            return MemoryEvidenceRelation.EXPLICIT_COMMAND
        if authority is MemoryAuthority.THIRD_PARTY:
            return MemoryEvidenceRelation.THIRD_PARTY_STATEMENT
        if authority is MemoryAuthority.GROUP_REPORT:
            return MemoryEvidenceRelation.GROUP_STATEMENT
        return MemoryEvidenceRelation.SELF_STATEMENT
