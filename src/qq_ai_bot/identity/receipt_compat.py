"""Shared live-receipt compatibility checks. Import-leaf for both UoWs."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from qq_ai_bot.conversation.canonical_db_models import CanonicalEventReceiptModel
from qq_ai_bot.domain.conversations import ConversationScope
from qq_ai_bot.identity.errors import CanonicalIdentityError
from qq_ai_bot.persistence.models import ChatEventModel

_KEEPER_STATUS = "keeper"


def normalize_live_text(value: object) -> str:
    return " ".join(str(value or "").split())


def _canonicalize_json_keys(value: object) -> object:
    if isinstance(value, dict):
        return {str(key): _canonicalize_json_keys(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_canonicalize_json_keys(item) for item in value]
    return value


def normalize_live_json(value: object) -> str:
    """Compare repository JSON by canonical key order, not raw object insertion order."""

    if value is None:
        raw = "[]"
    elif isinstance(value, str):
        raw = value
    else:
        raw = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if not raw.strip():
        return "[]"
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return raw
    return json.dumps(
        _canonicalize_json_keys(parsed),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )


def occurred_at_token(value: datetime | None) -> str:
    """Cutover duplicate classifier compares platform occurred_at as a stable token."""

    if value is None:
        return ""
    aware = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return aware.astimezone(UTC).isoformat()


async def load_claimed_keeper(
    session: AsyncSession, canonical_event_id: str | None
) -> ChatEventModel | None:
    """Deterministically load the unique keeper for a receipt canonical id."""

    if not canonical_event_id:
        return None
    return cast(
        ChatEventModel | None,
        await session.scalar(
            select(ChatEventModel).where(
                ChatEventModel.canonical_event_id == canonical_event_id,
                ChatEventModel.suppression_status == _KEEPER_STATUS,
            )
        ),
    )


def require_claimed_event(
    receipt: CanonicalEventReceiptModel | None,
    event: ChatEventModel | None,
) -> ChatEventModel:
    """Fail closed when a receipt exists without its keeper, or the pair is forged."""

    if receipt is None or event is None:
        raise CanonicalIdentityError("receipt_conflict")
    if event.canonical_event_id != receipt.canonical_event_id:
        raise CanonicalIdentityError("receipt_conflict")
    if event.suppression_status != _KEEPER_STATUS:
        raise CanonicalIdentityError("receipt_conflict")
    return event


def require_compatible_v2_live(
    existing: ChatEventModel,
    *,
    scope: ConversationScope,
    conversation_id: str,
    presence_id: str,
    platform_message_id: str,
    sender_user_id: str,
    direction: str,
    event_kind: str,
    content: str,
    segments: tuple[dict[str, Any], ...] | list[dict[str, Any]],
    timestamp: datetime,
    author_kind: str,
    author_person_id: str | None,
    author_presence_id: str | None,
    receipt: CanonicalEventReceiptModel | None,
    external_event_type: str | None,
) -> None:
    receipt_event_type = (external_event_type or "message")[:64]
    incoming_segments = json.dumps(segments, ensure_ascii=False, separators=(",", ":"))
    if existing.canonical_conversation_id != conversation_id:
        raise CanonicalIdentityError("receipt_conflict")
    if receipt is not None:
        if (
            receipt.ingress_presence_id != presence_id
            or receipt.event_type != receipt_event_type
            or receipt.platform_message_id != platform_message_id[:128]
            or receipt.canonical_event_id != existing.canonical_event_id
        ):
            raise CanonicalIdentityError("receipt_conflict")
    elif (
        existing.ingress_presence_id not in {None, presence_id}
        or existing.bot_user_id != scope.bot_user_id
    ):
        raise CanonicalIdentityError("receipt_conflict")
    if existing.event_kind != event_kind or existing.direction != direction:
        raise CanonicalIdentityError("receipt_conflict")
    if (
        existing.author_kind != author_kind
        or existing.author_person_id != author_person_id
        or existing.author_presence_id != author_presence_id
        or existing.sender_user_id != sender_user_id
    ):
        raise CanonicalIdentityError("receipt_conflict")
    if normalize_live_text(existing.content) != normalize_live_text(content):
        raise CanonicalIdentityError("receipt_conflict")
    if normalize_live_json(existing.segments_json) != normalize_live_json(incoming_segments):
        raise CanonicalIdentityError("receipt_conflict")
    if occurred_at_token(existing.occurred_at) != occurred_at_token(timestamp):
        raise CanonicalIdentityError("receipt_conflict")
    if (
        existing.scope_type != scope.scope_type.value
        or existing.group_id != scope.group_id
        or existing.private_peer_user_id != scope.private_peer_user_id
        or existing.platform_message_id != platform_message_id
    ):
        raise CanonicalIdentityError("receipt_conflict")


async def require_reply_source(
    session: AsyncSession, *, conversation_id: str, reply_to_event_id: int | None
) -> None:
    """Validate an already-resolved internal reply without a platform lookup."""
    if reply_to_event_id is None:
        return
    source = await session.get(ChatEventModel, reply_to_event_id)
    if source is None or source.canonical_conversation_id != conversation_id:
        raise CanonicalIdentityError("receipt_conflict")
