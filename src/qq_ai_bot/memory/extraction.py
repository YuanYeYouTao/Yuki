"""Narrow structured contract for one-event Memory V2 extraction."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.memory.enums import (
    MemoryClaimOperation,
    MemoryKind,
    MemoryScopeType,
    MemorySourceType,
    MemoryTemporalMode,
)
from qq_ai_bot.persistence.repository_records import EventRecord

EXTRACTION_PROMPT_VERSION = "memory-v2-extraction-v4"
EXTRACTION_SCHEMA_VERSION = "4"
SOURCE_ADAPTATION_VERSION = "3"
BATCH_EXTRACTION_PROMPT_VERSION = "memory-v2-batch-extraction-v3"
BATCH_EXTRACTION_SCHEMA_VERSION = "3"


class _ExtractionModel(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class PrimaryEvent(_ExtractionModel):
    scope_type: ScopeType
    content: str
    occurred_at: datetime


class ConversationContextEvent(_ExtractionModel):
    speaker_role: str = Field(pattern=r"^(current_speaker|other_member|bot)$")
    content: str


class AvailableSubject(_ExtractionModel):
    subject_ref: str
    display_label: str
    allowed_scopes: tuple[MemoryScopeType, ...]
    relation_to_speaker: str


class MemoryExtractionInput(_ExtractionModel):
    primary_event: PrimaryEvent
    available_subjects: tuple[AvailableSubject, ...]
    conversation_context: tuple[ConversationContextEvent, ...] = ()


class MemoryClaim(_ExtractionModel):
    operation: MemoryClaimOperation = MemoryClaimOperation.ASSERT
    subject_ref: str = Field(min_length=1)
    subject_name: str | None = Field(default=None, min_length=1)
    scope_type: MemoryScopeType
    kind: MemoryKind = MemoryKind.FACT
    memory_key: str = Field(min_length=1)
    category: str = Field(min_length=1)
    content: str = Field(min_length=1)
    evidence_quote: str = Field(min_length=1)
    importance: int = Field(default=3, ge=1, le=5)
    confidence: float = Field(default=0.8, ge=0, le=1)
    source_type: MemorySourceType = MemorySourceType.AUTOMATIC
    temporal_mode: MemoryTemporalMode = MemoryTemporalMode.PERSISTENT
    valid_from: str | None = None
    valid_until: str | None = None


ExtractedMemoryClaim = MemoryClaim


class MemoryExtractionOutput(_ExtractionModel):
    claims: tuple[ExtractedMemoryClaim, ...] = ()


class BatchPrimaryEvent(_ExtractionModel):
    source_event_id: int = Field(gt=0)
    scope_type: ScopeType
    sender_label: str = Field(min_length=1)
    content: str
    occurred_at: datetime
    available_subjects: tuple[AvailableSubject, ...]


class BatchConversationContextEvent(_ExtractionModel):
    speaker_role: str = Field(pattern=r"^(member|bot)$")
    sender_label: str = Field(min_length=1)
    content: str


class BatchMemoryExtractionInput(_ExtractionModel):
    events: tuple[BatchPrimaryEvent, ...] = Field(min_length=1)
    conversation_context: tuple[BatchConversationContextEvent, ...] = Field(
        default=(),
    )


class BatchMemoryClaim(_ExtractionModel):
    source_event_id: int = Field(gt=0)
    claim: ExtractedMemoryClaim


class BatchMemoryExtractionOutput(_ExtractionModel):
    claims: tuple[BatchMemoryClaim, ...] = Field(default=())


def source_event_fingerprint(event: EventRecord) -> str:
    """Hash immutable source semantics; derived visual text is deliberately excluded."""

    payload = {
        "event_id": event.id,
        "bot_user_id": event.bot_user_id,
        "platform_message_id": event.platform_message_id,
        "scope_type": event.scope_type.value,
        "sender_user_id": event.sender_user_id,
        "group_id": event.group_id,
        "private_peer_user_id": event.private_peer_user_id,
        "direction": event.direction,
        "content": event.evidence_content,
        "segments": event.segments,
        "reply_to_message_id": event.reply_to_message_id,
        "mentioned_user_ids": event.mentioned_user_ids,
        "reply_sender_user_id": event.reply_sender_user_id,
        "origin": event.origin,
        "occurred_at": event.occurred_at.isoformat(),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
