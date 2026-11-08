"""Shared live-receipt compatibility checks. Import-leaf for both UoWs."""

from __future__ import annotations

import json
from datetime import datetime
from typing import cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from yuki.conversation.canonical_db_models import CanonicalEventReceiptModel
from yuki.domain.conversations import ConversationScope
from yuki.identity.db_models import AccountAssignmentModel
from yuki.identity.errors import CanonicalIdentityError
from yuki.persistence.models import ChatEventModel
from yuki.time.formatting import utc_iso

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

    return "" if value is None else utc_iso(value)


def transport_namespace(scope: ConversationScope) -> str:
    """Native chat/topic namespace; no owner, connection or Conversation ID."""
    return json.dumps(
        [scope.scope_type.value, scope.group_id or scope.private_peer_user_id, scope.thread_id],
        ensure_ascii=False,
        separators=(",", ":"),
    )


async def transport_receipt(
    session: AsyncSession,
    *,
    presence_id: str,
    scope: ConversationScope,
    event_type: str,
    ingress_key: str | None,
) -> CanonicalEventReceiptModel | None:
    account = (
        select(AccountAssignmentModel.account_id)
        .where(AccountAssignmentModel.id == presence_id)
        .scalar_subquery()
    )
    return cast(
        CanonicalEventReceiptModel | None,
        await session.scalar(
            select(CanonicalEventReceiptModel).where(
                CanonicalEventReceiptModel.ingress_account_id == account,
                CanonicalEventReceiptModel.transport_namespace == transport_namespace(scope),
                CanonicalEventReceiptModel.event_type == event_type,
                CanonicalEventReceiptModel.ingress_key == ingress_key,
            )
        ),
    )


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
    candidate: ChatEventModel,
    *,
    receipt: CanonicalEventReceiptModel | None,
    event_type: str,
    normalized_segments: str | None = None,
) -> None:
    """Fail closed unless ``existing`` is the same event as the ``candidate`` row.

    Authors are not compared: the original receipt keeps the author it accepted.
    A candidate without ``occurred_at`` accepts the original time.
    """
    if normalized_segments is None:
        normalized_segments = normalize_live_json(candidate.segments_json)
    if existing.canonical_conversation_id != candidate.canonical_conversation_id:
        raise CanonicalIdentityError("receipt_conflict")
    if receipt is not None:
        if (
            receipt.event_type != event_type[:64]
            or receipt.ingress_key != candidate.origin_key
            or receipt.canonical_event_id != existing.canonical_event_id
        ):
            raise CanonicalIdentityError("receipt_conflict")
    elif (
        existing.ingress_presence_id not in {None, candidate.ingress_presence_id}
        or existing.bot_user_id != candidate.bot_user_id
    ):
        raise CanonicalIdentityError("receipt_conflict")
    if (
        existing.event_kind != candidate.event_kind
        or existing.direction != candidate.direction
        or existing.sender_user_id != candidate.sender_user_id
        or existing.scope_type != candidate.scope_type
        or existing.group_id != candidate.group_id
        or existing.private_peer_user_id != candidate.private_peer_user_id
        or existing.origin_key != candidate.origin_key
    ):
        raise CanonicalIdentityError("receipt_conflict")
    if normalize_live_text(existing.content) != normalize_live_text(candidate.content):
        raise CanonicalIdentityError("receipt_conflict")
    if normalize_live_json(existing.segments_json) != normalized_segments:
        raise CanonicalIdentityError("receipt_conflict")
    if candidate.occurred_at is not None and occurred_at_token(
        existing.occurred_at
    ) != occurred_at_token(candidate.occurred_at):
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
