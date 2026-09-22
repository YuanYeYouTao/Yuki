"""One event eligibility policy shared by live extraction and historical rebuild."""

from __future__ import annotations

from sqlalchemy import exists, func, not_, or_, select

from qq_ai_bot.domain.identity import AuthorKind
from qq_ai_bot.memory.enums import MemoryJobStatus
from qq_ai_bot.memory.self_origin import sql_self_receipt_evidence_predicate
from qq_ai_bot.persistence.models import ChatEventModel, MemoryJobModel
from qq_ai_bot.persistence.repository_helpers import (
    keeper_event_clause,
    sql_keeper_event_predicate,
    suppression_is_canonical_live,
)
from qq_ai_bot.persistence.repository_records import EventRecord

_REJECTED_AUTHOR_KINDS = frozenset(
    {
        AuthorKind.YUKI.value,
        AuthorKind.EXTERNAL_BOT.value,
        AuthorKind.SYSTEM.value,
    }
)


def sql_human_evidence_predicate(alias: str = "c") -> str:
    """Raw-SQL canonical human evidence predicate."""

    return f"{alias}.author_kind='person' AND {sql_keeper_event_predicate(alias)}"


def sql_fact_tool_evidence_predicate() -> str:
    """Retained SELF receipt chain: aliases f/e/t/c/v are fact/evidence/receipt/event/conversation.

    Expiry limits new reflection input, not evidence already committed. Failed
    tool results may document a failure; success is not an evidence requirement.
    """
    event_source = (
        "(t.initiative_run_id IS NULL AND t.trigger_event_id IS NOT NULL "
        "AND e.event_id IS NULL AND f.scope_type='self' AND e.relation='agent_reflection' "
        "AND e.authority='agent_reflection' AND e.source_speaker_user_id=t.bot_user_id "
        "AND trim(e.excerpt)!='' AND instr(t.result_excerpt,e.excerpt)>0 "
        "AND c.canonical_event_id IS NOT NULL "
        f"AND {sql_keeper_event_predicate('c')} "
        "AND ((c.direction='inbound' AND c.author_kind='person') "
        "OR (c.direction='outbound' AND c.author_kind='yuki')) "
        "AND ((t.canonical_person_id=v.person_id AND t.canonical_space_id IS NULL) "
        "OR (t.canonical_space_id=v.space_id AND t.canonical_person_id IS NULL)) "
        "AND ((f.visibility_type='global' AND f.canonical_visibility_person_id IS NULL "
        "AND f.canonical_visibility_space_id IS NULL) "
        "OR (f.visibility_type='private' AND f.canonical_visibility_person_id=v.person_id "
        "AND f.canonical_visibility_space_id IS NULL) "
        "OR (f.visibility_type='group' AND f.canonical_visibility_space_id=v.space_id "
        "AND f.canonical_visibility_person_id IS NULL)))"
    )
    return f"COALESCE(({event_source} OR {sql_self_receipt_evidence_predicate()}), 0)"


class MemoryEventEligibilityPolicy:
    """Keep domain and SQL event eligibility intentionally equivalent."""

    allowed_origins = frozenset({"user_message", "onebot_history"})

    def is_eligible(self, event: EventRecord) -> bool:
        return self.rejection_reason(event) is None

    def rejection_reason(
        self,
        event: EventRecord,
    ) -> str | None:
        """Return a stable, content-free reason when an event cannot be queued."""

        if event.direction != "inbound":
            return "not_inbound"
        if event.author_kind in _REJECTED_AUTHOR_KINDS:
            return "bot_sender"
        if event.author_kind != AuthorKind.PERSON.value:
            return "bot_sender"
        if not suppression_is_canonical_live(event.suppression_status):
            return "suppressed_duplicate"
        if not event.evidence_content.strip():
            return "blank_content"
        if event.origin not in self.allowed_origins:
            return "unsupported_origin"
        if event.scope_type.value not in {"private", "group"}:
            return "unsupported_scope"
        if event.scope_type.value == "group" and not event.group_id:
            return "group_id_missing"
        if event.scope_type.value == "private" and not event.private_peer_user_id:
            return "private_peer_missing"
        return None

    def sql_conditions(self, *, include_failed_live_jobs: bool) -> tuple[object, ...]:
        excluded = [
            MemoryJobStatus.DONE.value,
            MemoryJobStatus.PENDING.value,
            MemoryJobStatus.PROCESSING.value,
        ]
        if not include_failed_live_jobs:
            excluded.append(MemoryJobStatus.FAILED.value)
        receipt = exists(
            select(MemoryJobModel.id).where(
                MemoryJobModel.event_id == ChatEventModel.id,
                MemoryJobModel.status.in_(excluded),
            )
        )
        person_author = ChatEventModel.author_kind == AuthorKind.PERSON.value
        not_suppressed = keeper_event_clause()
        return (
            ChatEventModel.direction == "inbound",
            person_author,
            not_suppressed,
            or_(
                func.length(func.trim(ChatEventModel.content)) > 0,
                ChatEventModel.audio_transcript.contains('"source":"current"'),
            ),
            ChatEventModel.origin.in_(tuple(self.allowed_origins)),
            ChatEventModel.scope_type.in_(("private", "group")),
            or_(ChatEventModel.scope_type != "group", ChatEventModel.group_id.is_not(None)),
            or_(
                ChatEventModel.scope_type != "private",
                ChatEventModel.private_peer_user_id.is_not(None),
            ),
            not_(receipt),
        )
