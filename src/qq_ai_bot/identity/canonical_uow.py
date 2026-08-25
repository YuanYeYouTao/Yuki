"""v2 fence + receipt claim + canonical append in one BEGIN IMMEDIATE."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import cast
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from qq_ai_bot.conversation.canonical_db_models import (
    CanonicalConversationModel,
    CanonicalEventReceiptModel,
)
from qq_ai_bot.conversation.hydrate import (
    bump_canonical_generation,
    hydrate_scope_state_from_canonical,
    touch_canonical_watermarks,
)
from qq_ai_bot.conversation.rollup.models import RollupPolicyConfig
from qq_ai_bot.domain.conversations import ConversationScope, ScopeType
from qq_ai_bot.domain.identity import AuthorKind
from qq_ai_bot.domain.messages import InboundMessage
from qq_ai_bot.identity.dual_write import trip
from qq_ai_bot.identity.errors import IdentityDualWriteError
from qq_ai_bot.identity.ingress import IngressPreAdmit
from qq_ai_bot.identity.receipt_compat import (
    load_claimed_keeper,
    require_claimed_event,
    require_compatible_v2_live,
)
from qq_ai_bot.identity.routing import PresenceRouter
from qq_ai_bot.identity.runtime import require_complete_v2_runtime
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.models import ChatEventModel
from qq_ai_bot.persistence.repository_helpers import _event_record
from qq_ai_bot.persistence.scoped_event_uow import NewGenerationResult, ScopedAppendResult


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _new_id() -> str:
    return str(uuid4())


def _fingerprint(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


class CanonicalIngressUnitOfWork:
    """The only v2 writer that claims receipts and appends canonical events."""

    def __init__(
        self,
        database: Database,
        router: PresenceRouter,
        *,
        config: RollupPolicyConfig | None = None,
    ) -> None:
        self._database = database
        self._router = router
        self._config = config or RollupPolicyConfig()

    async def append_inbound(
        self,
        message: InboundMessage,
        admitted: IngressPreAdmit,
        *,
        new_generation: bool = False,
    ) -> ScopedAppendResult:
        if admitted.dropped or admitted.presence_id is None or admitted.conversation_id is None:
            raise IdentityDualWriteError("unclassified")
        if not admitted.provider or not admitted.handle_external_account_id:
            raise IdentityDualWriteError("unclassified")
        if message.bot_user_id and message.bot_user_id != admitted.handle_external_account_id:
            raise IdentityDualWriteError("bot_handle_mismatch")
        scope = (
            ConversationScope.private(admitted.handle_external_account_id, message.sender.user_id)
            if message.scope_type is ScopeType.PRIVATE
            else ConversationScope.group(
                admitted.handle_external_account_id, message.group_id or ""
            )
        )
        segments = list(message.segments)
        segments.append(
            {
                "type": "yuki_context",
                "data": {
                    "mentioned_user_ids": list(message.mentioned_user_ids),
                    "reply_sender_user_id": message.reply_sender_user_id,
                },
            }
        )
        now = _utcnow()
        created = True
        async with self._database.immediate_session() as session:
            await require_complete_v2_runtime(session)
            trip("before_fence_recheck")
            if message.scope_type is ScopeType.GROUP:
                if admitted.space_binding_id is None or admitted.presence_id is None:
                    raise IdentityDualWriteError("unclassified")
                fence = await self._router.evaluate_ingest(
                    space_binding_id=admitted.space_binding_id,
                    event_presence_id=admitted.presence_id,
                )
                if fence != "ok":
                    raise IdentityDualWriteError(fence)
            trip("after_fence_recheck")
            existing = await _existing_claimed_event(
                session,
                presence_id=admitted.presence_id,
                event_type=message.event_type[:64],
                platform_message_id=message.message_id[:128],
            )
            if existing is not None:
                return await _reuse_claimed_inbound(
                    session,
                    existing,
                    message=message,
                    admitted=admitted,
                    scope=scope,
                    segments=segments,
                )
            canonical_event_id = _new_id()
            try:
                async with session.begin_nested():
                    session.add(
                        CanonicalEventReceiptModel(
                            ingress_presence_id=admitted.presence_id,
                            event_type=message.event_type[:64],
                            platform_message_id=message.message_id[:128],
                            canonical_event_id=canonical_event_id,
                            created_at=now,
                            observed_at=now,
                        )
                    )
                    await session.flush()
            except IntegrityError as exc:
                raced = await _existing_claimed_event(
                    session,
                    presence_id=admitted.presence_id,
                    event_type=message.event_type[:64],
                    platform_message_id=message.message_id[:128],
                )
                if raced is None:
                    raise IdentityDualWriteError("receipt_conflict") from exc
                return await _reuse_claimed_inbound(
                    session,
                    raced,
                    message=message,
                    admitted=admitted,
                    scope=scope,
                    segments=segments,
                )
            trip("after_receipt_claim")
            row = ChatEventModel(
                bot_user_id=scope.bot_user_id,
                platform_message_id=message.message_id,
                scope_type=scope.scope_type.value,
                group_id=scope.group_id,
                private_peer_user_id=scope.private_peer_user_id,
                sender_user_id=message.sender.user_id,
                sender_nickname=message.sender.nickname[:128],
                sender_group_card=message.sender.group_card[:128],
                direction="inbound",
                event_kind="message",
                content=message.text,
                visual_summary="",
                segments_json=json.dumps(segments, ensure_ascii=False, separators=(",", ":")),
                reply_to_message_id=message.reply_to_message_id,
                origin="user_message",
                occurred_at=message.received_at,
                observed_at=now,
                canonical_event_id=canonical_event_id,
                canonical_conversation_id=admitted.conversation_id,
                author_kind=admitted.author_kind,
                author_person_id=admitted.author_person_id,
                author_presence_id=admitted.author_presence_id,
                ingress_presence_id=admitted.presence_id,
                utterance_fingerprint=_fingerprint(message.text),
                suppression_status="keeper",
                ingress_provider=admitted.provider,
                ingress_gateway_instance_id=admitted.gateway_instance_id,
            )
            _validate_author_shape(row)
            session.add(row)
            await session.flush()
            event = _event_record(row)
            await touch_canonical_watermarks(
                session,
                admitted.conversation_id,
                event_id=row.id,
                characters=len(message.text),
            )
            if new_generation:
                await bump_canonical_generation(
                    session,
                    admitted.conversation_id,
                    event_id=row.id,
                )
            conversation = await session.get(CanonicalConversationModel, admitted.conversation_id)
            if conversation is None:
                raise IdentityDualWriteError("unclassified")
            from qq_ai_bot.conversation.canonical_rollup import signal_canonical_rollup_if_needed

            signalled = await signal_canonical_rollup_if_needed(
                session, conversation, self._config, force_existing=True
            )
            trip("after_canonical_append")
            state = await hydrate_scope_state_from_canonical(session, scope, conversation)
        return ScopedAppendResult(
            event=event, scope=state, created=created, job_signalled=signalled
        )

    async def append_new_generation(
        self,
        message: InboundMessage,
        admitted: IngressPreAdmit,
    ) -> NewGenerationResult:
        appended = await self.append_inbound(message, admitted, new_generation=True)
        return NewGenerationResult(
            event=appended.event,
            scope=appended.scope,
            generation_changed=appended.scope.last_generation_change_event_id == appended.event.id,
        )


async def _load_receipt(
    session: AsyncSession,
    *,
    presence_id: str,
    event_type: str,
    platform_message_id: str,
) -> CanonicalEventReceiptModel | None:
    return cast(
        CanonicalEventReceiptModel | None,
        await session.scalar(
            select(CanonicalEventReceiptModel).where(
                CanonicalEventReceiptModel.ingress_presence_id == presence_id,
                CanonicalEventReceiptModel.event_type == event_type,
                CanonicalEventReceiptModel.platform_message_id == platform_message_id,
            )
        ),
    )


async def _existing_claimed_event(
    session: AsyncSession,
    *,
    presence_id: str,
    event_type: str,
    platform_message_id: str,
) -> ChatEventModel | None:
    receipt = await _load_receipt(
        session,
        presence_id=presence_id,
        event_type=event_type,
        platform_message_id=platform_message_id,
    )
    if receipt is None:
        return None
    claimed = await load_claimed_keeper(session, receipt.canonical_event_id)
    return require_claimed_event(receipt, claimed)


async def _reuse_claimed_inbound(
    session: AsyncSession,
    existing: ChatEventModel,
    *,
    message: InboundMessage,
    admitted: IngressPreAdmit,
    scope: ConversationScope,
    segments: list[dict[str, object]],
) -> ScopedAppendResult:
    if admitted.conversation_id is None or admitted.presence_id is None:
        raise IdentityDualWriteError("unclassified")
    conversation = await session.get(CanonicalConversationModel, admitted.conversation_id)
    if conversation is None:
        raise IdentityDualWriteError("unclassified")
    receipt = await _load_receipt(
        session,
        presence_id=admitted.presence_id,
        event_type=message.event_type[:64],
        platform_message_id=message.message_id[:128],
    )
    require_claimed_event(receipt, existing)
    require_compatible_v2_live(
        existing,
        scope=scope,
        conversation_id=admitted.conversation_id,
        presence_id=admitted.presence_id,
        platform_message_id=message.message_id,
        sender_user_id=message.sender.user_id,
        direction="inbound",
        event_kind="message",
        content=message.text,
        segments=segments,
        timestamp=message.received_at,
        author_kind=admitted.author_kind,
        author_person_id=admitted.author_person_id,
        author_presence_id=admitted.author_presence_id,
        receipt=receipt,
        external_event_type=message.event_type,
    )
    return ScopedAppendResult(
        event=_event_record(existing),
        scope=await hydrate_scope_state_from_canonical(session, scope, conversation),
        created=False,
        job_signalled=False,
    )


def _validate_author_shape(row: ChatEventModel) -> None:
    kind = row.author_kind
    if kind == AuthorKind.PERSON.value:
        if row.author_person_id is None or row.author_presence_id is not None:
            raise IdentityDualWriteError("unclassified")
        return
    if kind == AuthorKind.YUKI.value:
        if row.author_presence_id is None or row.author_person_id is not None:
            raise IdentityDualWriteError("unclassified")
        return
    if kind in {AuthorKind.EXTERNAL_BOT.value, AuthorKind.SYSTEM.value}:
        if row.author_person_id is not None or row.author_presence_id is not None:
            raise IdentityDualWriteError("unclassified")
        return
    raise IdentityDualWriteError("unclassified")
