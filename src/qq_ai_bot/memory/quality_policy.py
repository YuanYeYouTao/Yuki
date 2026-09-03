"""Structured attribution and retention policy for extracted memory claims.

The extractor owns semantic interpretation.  This module deliberately avoids
reading Chinese wording to second-guess the model; it only verifies that the
declared enums agree with backend-owned subject references and event metadata.
"""

from __future__ import annotations

from dataclasses import dataclass

from qq_ai_bot.memory.enums import (
    MemoryRetention,
    MemoryScopeType,
    MemorySourceStyle,
    MemorySubjectBasis,
)
from qq_ai_bot.memory.extraction import MemoryClaim
from qq_ai_bot.memory.subjects import ResolvedSubject
from qq_ai_bot.persistence.repository_records import EventRecord


@dataclass(frozen=True, slots=True)
class MemoryPolicyDecision:
    accepted: bool
    reason_code: str
    candidate_type: str | None = None

    @classmethod
    def accept(cls) -> MemoryPolicyDecision:
        return cls(True, "policy_verified")

    @classmethod
    def reject(cls, reason_code: str) -> MemoryPolicyDecision:
        return cls(False, reason_code)

    @classmethod
    def candidate(cls, reason_code: str, candidate_type: str) -> MemoryPolicyDecision:
        return cls(False, reason_code, candidate_type)


class AttributionPolicy:
    """Check model declarations against trusted references without parsing prose."""

    @staticmethod
    def evaluate(
        claim: MemoryClaim,
        event: EventRecord,
        resolved: ResolvedSubject | None,
    ) -> MemoryPolicyDecision:
        del event
        basis = claim.subject_basis
        if basis is MemorySubjectBasis.ABOUT_YUKI:
            return MemoryPolicyDecision.candidate(
                "self_candidate_requires_agent_judgment",
                "self",
            )
        if resolved is None:
            if claim.subject_ref == "named_member" or basis is MemorySubjectBasis.NAMED_UNRESOLVED:
                return MemoryPolicyDecision.candidate("named_subject_unresolved", "memory")
            return MemoryPolicyDecision.reject("subject_unresolved")
        if claim.scope_type is MemoryScopeType.SELF:
            return MemoryPolicyDecision.reject("self_candidate_requires_agent_judgment")
        if resolved.scope_type is MemoryScopeType.GROUP:
            return (
                MemoryPolicyDecision.accept()
                if basis is MemorySubjectBasis.GROUP
                else MemoryPolicyDecision.reject("group_basis_not_verified")
            )
        if claim.subject_ref == "speaker":
            return (
                MemoryPolicyDecision.accept()
                if basis
                in {
                    MemorySubjectBasis.FIRST_PERSON,
                    MemorySubjectBasis.OMITTED_SELF,
                }
                else MemoryPolicyDecision.reject("speaker_basis_not_verified")
            )
        if claim.subject_ref.startswith("mentioned_"):
            return (
                MemoryPolicyDecision.accept()
                if basis
                in {
                    MemorySubjectBasis.MENTIONED_SUBJECT,
                    MemorySubjectBasis.ADDRESSED_SECOND_PERSON,
                    MemorySubjectBasis.NAMED_UNRESOLVED,
                }
                else MemoryPolicyDecision.reject("mentioned_subject_basis_mismatch")
            )
        if claim.subject_ref == "reply_author":
            return (
                MemoryPolicyDecision.accept()
                if basis
                in {
                    MemorySubjectBasis.REPLY_SUBJECT,
                    MemorySubjectBasis.ADDRESSED_SECOND_PERSON,
                    MemorySubjectBasis.NAMED_UNRESOLVED,
                }
                else MemoryPolicyDecision.reject("reply_subject_basis_mismatch")
            )
        if claim.subject_ref.startswith("named_"):
            return (
                MemoryPolicyDecision.accept()
                if basis is MemorySubjectBasis.NAMED_UNRESOLVED
                else MemoryPolicyDecision.reject("named_subject_basis_mismatch")
            )
        return MemoryPolicyDecision.reject("subject_basis_not_verified")


class RetentionPolicy:
    """Apply the extractor's structured retention decision without text heuristics."""

    @staticmethod
    def evaluate(
        claim: MemoryClaim,
        event: EventRecord,
        *,
        explicit_request: bool,
    ) -> MemoryPolicyDecision:
        if explicit_request:
            return MemoryPolicyDecision.accept()
        if claim.retention is MemoryRetention.TRANSIENT:
            return MemoryPolicyDecision.reject("transient_not_long_term")
        style = claim.source_style
        if style is MemorySourceStyle.ROLEPLAY:
            return MemoryPolicyDecision.reject("roleplay_not_long_term_preference")
        if style is MemorySourceStyle.INSTRUCTION:
            return MemoryPolicyDecision.reject("instruction_not_long_term_preference")
        if style is MemorySourceStyle.GENERATED_RESULT:
            return MemoryPolicyDecision.reject("generated_result_not_self_report")
        if style is MemorySourceStyle.QUOTED_TEXT:
            return MemoryPolicyDecision.reject("quoted_text_not_self_report")
        if event.direction != "inbound":
            return MemoryPolicyDecision.reject("generated_result_not_self_report")
        return MemoryPolicyDecision.accept()


class AutomaticValuePolicy:
    """One structured admission gate, never a second semantic classifier."""

    @staticmethod
    def evaluate(
        *, importance: int, retention: MemoryRetention, value_reason: str
    ) -> MemoryPolicyDecision:
        if retention is MemoryRetention.TRANSIENT:
            return MemoryPolicyDecision.reject("transient_not_long_term")
        if importance < 3:
            return MemoryPolicyDecision.reject("low_long_term_value")
        if not value_reason.strip():
            return MemoryPolicyDecision.reject("automatic_value_reason_required")
        return MemoryPolicyDecision.accept()
