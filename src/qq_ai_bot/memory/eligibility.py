"""One event eligibility policy shared by live extraction and historical rebuild."""

from __future__ import annotations

from sqlalchemy import and_, exists, func, not_, or_, select
from sqlalchemy.sql.elements import ColumnElement

from qq_ai_bot.domain.identity import AuthorKind
from qq_ai_bot.memory.enums import MemoryJobStatus
from qq_ai_bot.persistence.models import ChatEventModel, MemoryJobModel, PersonModel
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


def _legacy_v1_null_author_human_sql(*, bot_sender: ColumnElement[bool]) -> ColumnElement[bool]:
    """author_kind IS NULL v1 human: sender!=bot and not people.is_bot."""

    return and_(
        ChatEventModel.author_kind.is_(None),
        ChatEventModel.sender_user_id != ChatEventModel.bot_user_id,
        not_(bot_sender),
    )


def sql_human_evidence_predicate(alias: str = "c") -> str:
    """Raw-SQL human evidence: person, or author_kind-is-None v1 fallback."""

    return (
        f"({alias}.author_kind='person' OR ("
        f"{alias}.author_kind IS NULL AND {alias}.sender_user_id!={alias}.bot_user_id "
        f"AND NOT EXISTS (SELECT 1 FROM people p "
        f"WHERE p.user_id={alias}.sender_user_id AND p.is_bot=1)"
        f")) AND {sql_keeper_event_predicate(alias)}"
    )


class MemoryEventEligibilityPolicy:
    """Keep domain and SQL event eligibility intentionally equivalent."""

    allowed_origins = frozenset({"user_message", "onebot_history"})

    def is_eligible(self, event: EventRecord, *, sender_is_bot: bool = False) -> bool:
        return self.rejection_reason(event, sender_is_bot=sender_is_bot) is None

    def rejection_reason(
        self,
        event: EventRecord,
        *,
        sender_is_bot: bool = False,
    ) -> str | None:
        """Return a stable, content-free reason when an event cannot be queued."""

        if event.direction != "inbound":
            return "not_inbound"
        if event.author_kind in _REJECTED_AUTHOR_KINDS:
            return "bot_sender"
        if event.author_kind == AuthorKind.PERSON.value:
            pass
        elif event.author_kind is None:
            if sender_is_bot or event.sender_user_id == event.bot_user_id:
                return "bot_sender"
        else:
            return "bot_sender"
        if not suppression_is_canonical_live(event.suppression_status):
            return "suppressed_duplicate"
        if not event.content.strip():
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
        bot_sender = exists(
            select(PersonModel.user_id).where(
                PersonModel.user_id == ChatEventModel.sender_user_id,
                PersonModel.is_bot.is_(True),
            )
        )
        person_author = ChatEventModel.author_kind == AuthorKind.PERSON.value
        legacy_human = _legacy_v1_null_author_human_sql(bot_sender=bot_sender)
        not_suppressed = keeper_event_clause()
        return (
            ChatEventModel.direction == "inbound",
            or_(person_author, legacy_human),
            not_suppressed,
            func.length(func.trim(ChatEventModel.content)) > 0,
            ChatEventModel.origin.in_(tuple(self.allowed_origins)),
            ChatEventModel.scope_type.in_(("private", "group")),
            or_(ChatEventModel.scope_type != "group", ChatEventModel.group_id.is_not(None)),
            or_(
                ChatEventModel.scope_type != "private",
                ChatEventModel.private_peer_user_id.is_not(None),
            ),
            not_(receipt),
        )
