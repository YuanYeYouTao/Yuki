"""Immutable projections returned by persistence repositories."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from qq_ai_bot.domain.audio import transcript_context
from qq_ai_bot.domain.conversations import ConversationScope, ScopeType
from qq_ai_bot.domain.identity import AuthorKind
from qq_ai_bot.domain.messages import sanitize_display_name


def event_author_is_yuki(*, author_kind: str | None) -> bool:
    """Return whether the canonical author classification is Yuki."""

    return author_kind == AuthorKind.YUKI.value


def event_author_is_human(*, author_kind: str | None) -> bool:
    """Return whether the canonical author classification is a Person."""

    return author_kind == AuthorKind.PERSON.value


@dataclass(frozen=True, slots=True)
class GroupSetting:
    """Domain projection of a group setting row."""

    group_id: str
    enabled: bool
    require_mention: bool
    autonomous_enabled: bool = True
    name: str = ""


@dataclass(frozen=True, slots=True)
class PrivateUserSetting:
    """Domain projection of one private-chat access state."""

    user_id: str
    enabled: bool


@dataclass(frozen=True, slots=True)
class EventRecord:
    """One permanent ledger event."""

    id: int
    bot_user_id: str
    platform_message_id: str
    scope_type: ScopeType
    sender_user_id: str
    direction: str
    content: str
    visual_summary: str
    segments: tuple[dict[str, Any], ...]
    occurred_at: datetime
    audio_transcript: str = ""
    sender_nickname: str = ""
    sender_group_card: str = ""
    group_id: str | None = None
    private_peer_user_id: str | None = None
    reply_to_message_id: str | None = None
    origin: str = "user_message"
    automation_id: int | None = None
    automation_run_id: int | None = None
    mentioned_user_ids: tuple[str, ...] = ()
    reply_sender_user_id: str | None = None
    event_kind: str = "message"
    source_plugin_id: str | None = None
    external_source: str | None = None
    external_event_key: str | None = None
    external_event_type: str | None = None
    external_payload: dict[str, Any] | None = None
    canonical_conversation_id: str | None = None
    canonical_event_id: str | None = None
    author_kind: str | None = None
    author_person_id: str | None = None
    author_presence_id: str | None = None
    ingress_presence_id: str | None = None
    suppression_status: str | None = None
    caused_by_event_id: int | None = None
    caused_by_external_source: str | None = None
    caused_by_external_event_type: str | None = None

    @property
    def scope(self) -> ConversationScope:
        """Reconstruct the exact bot-aware conversation identity."""

        if self.scope_type is ScopeType.GROUP:
            return ConversationScope.group(self.bot_user_id, self.group_id or "")
        return ConversationScope.private(
            self.bot_user_id,
            self.private_peer_user_id or self.sender_user_id,
        )

    @property
    def perceived_content(self) -> str:
        """Original text plus labelled ASR, keeping the ingress text immutable."""
        return (
            f"{self.content}\n{transcript_context(self.audio_transcript)}".strip()
            if self.audio_transcript
            else self.content
        )

    @property
    def evidence_content(self) -> str:
        """Quoted speech is context, never evidence authored by this event's sender."""
        if not self.audio_transcript:
            return self.content
        speech = transcript_context(self.audio_transcript, include_replies=False)
        return f"{self.content}\n{speech}".strip()

    def author_is_yuki(self) -> bool:
        return event_author_is_yuki(author_kind=self.author_kind)

    def author_is_human(self) -> bool:
        return event_author_is_human(author_kind=self.author_kind)

    @property
    def sender_display_name(self) -> str:
        """Return the immutable event-time display identity without a database lookup."""

        group_card = sanitize_display_name(self.sender_group_card)
        if group_card:
            return group_card
        nickname = sanitize_display_name(self.sender_nickname)
        if nickname:
            return nickname
        if self.author_kind == AuthorKind.YUKI.value:
            return "Yuki"
        if self.author_kind == AuthorKind.EXTERNAL_BOT.value:
            return "external bot"
        if self.author_kind == AuthorKind.SYSTEM.value:
            return "system"
        return f"QQ {self.sender_user_id}"


@dataclass(frozen=True, slots=True)
class MediaAnalysisRecord:
    """A cached structured observation; it never contains source image bytes."""

    id: int
    source_event_id: int | None
    segment_index: int
    content_hash: str
    analysis_mode: str
    question_hash: str
    provider: str
    model: str
    prompt_version: str
    observation_json: str
    created_at: datetime
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class EmojiDescriptionRecord:
    """A persistent, reusable description of one stable QQ emoji identity."""

    id: int
    emoji_key: str
    analysis_mode: str
    question_hash: str
    provider: str
    model: str
    prompt_version: str
    description: str
    observation_json: str
    hit_count: int
    created_at: datetime
    updated_at: datetime
    last_used_at: datetime


@dataclass(frozen=True, slots=True)
class RelationshipEventRecord:
    """One relationship change without duplicated chat content."""

    id: int
    user_id: str
    change_type: str
    affection_before: int
    affection_delta: int
    affection_after: int
    trust_before: int
    trust_delta: int
    trust_after: int
    reason_code: str
    confidence: float | None
    created_at: datetime
    source_event_id: int | None = None
    actor_user_id: str | None = None


@dataclass(frozen=True, slots=True)
class RelationshipJobRecord:
    """A claimed relationship job with bounded person-specific context."""

    job_id: int
    attempts: int
    user_id: str
    conversation_key: str
    trigger_event: EventRecord
    recent_events: tuple[EventRecord, ...]
