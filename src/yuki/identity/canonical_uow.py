"""Canonical receipt, event and local observation in one WAL transaction."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from functools import partial
from typing import cast
from uuid import uuid4

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from yuki.conversation.canonical_db_models import (
    CanonicalConversationModel,
    CanonicalEventReceiptModel,
)
from yuki.conversation.hydrate import (
    bump_canonical_generation,
    hydrate_scope_state_from_canonical,
    touch_canonical_watermarks,
)
from yuki.conversation.media_index import reply_media_snapshots
from yuki.domain.conversations import ConversationScope, ScopeType
from yuki.domain.identity import AuthorKind
from yuki.domain.messages import InboundMessage, snapshot_mention_segments
from yuki.domain.profiles import sanitize_profile_name
from yuki.gateway.local_clients import LOCAL_CLIENT_PLATFORMS
from yuki.identity.authority import person_is_superuser
from yuki.identity.canonical_repository import find_identity_binding, trip
from yuki.identity.db_models import (
    CanonicalSpaceModel,
    IdentityBindingModel,
    PresenceModel,
    SpaceBindingModel,
)
from yuki.identity.errors import CanonicalIdentityError
from yuki.identity.ingress import IngressPreAdmit
from yuki.identity.person_state import (
    apply_canonical_space_observation,
    prepare_canonical_person_observation,
    prepare_message_account_observations,
)
from yuki.identity.receipt_compat import (
    load_claimed_keeper,
    normalize_live_json,
    normalize_live_text,
    require_claimed_event,
    require_compatible_v2_live,
    require_reply_source,
    transport_namespace,
    transport_receipt,
)
from yuki.identity.routing import PresenceRouter
from yuki.persistence.database import Database
from yuki.persistence.history_search import prepare_terms
from yuki.persistence.history_search_index import replace_event
from yuki.persistence.models import ChatEventModel
from yuki.persistence.repository_helpers import _event_record
from yuki.persistence.repository_records import EventRecord
from yuki.persistence.scoped_event_uow import NewGenerationResult, ScopedAppendResult


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
    ) -> None:
        self._database = database
        self._router = router

    async def record_account_fact(
        self, message: InboundMessage, admitted: IngressPreAdmit
    ) -> EventRecord:
        """Save an unproved private author once, without inventing a Conversation."""
        now = _utcnow()
        scope = message.scope(bot_user_id=admitted.handle_external_account_id)
        canonical_id = _new_id()
        segments = list(snapshot_mention_segments(message.segments, message.mentions))
        segments.append(
            {
                "type": "yuki_context",
                "data": {
                    "platform": message.platform,
                    "native_occurred_at": message.occurred_at.isoformat()
                    if message.occurred_at
                    else None,
                },
            }
        )

        async def record(session: AsyncSession) -> EventRecord:
            existing = await _existing_claimed_event(
                session,
                scope=scope,
                presence_id=cast(str, admitted.presence_id),
                event_type=message.event_type,
                platform_message_id=message.message_id,
            )
            if existing is not None:
                original_parts = json.loads(existing.segments_json)
                if (
                    existing.author_account_id != admitted.author_account_id
                    or existing.reply_to_message_id != message.reply_to_message_id
                    or _native_payload(original_parts, existing.content)
                    != _native_payload(segments, message.text)
                ):
                    raise CanonicalIdentityError("receipt_conflict")
                require_compatible_v2_live(
                    existing,
                    scope=scope,
                    conversation_id=None,
                    presence_id=cast(str, admitted.presence_id),
                    platform_message_id=message.message_id,
                    sender_user_id=message.sender.user_id,
                    direction="inbound",
                    event_kind="message",
                    content=existing.content,
                    segments=original_parts,
                    timestamp=message.occurred_at or existing.occurred_at,
                    author_kind=AuthorKind.ACCOUNT.value,
                    author_person_id=None,
                    author_presence_id=None,
                    receipt=await _load_receipt(
                        session,
                        presence_id=cast(str, admitted.presence_id),
                        scope=scope,
                        event_type=message.event_type,
                        platform_message_id=message.message_id,
                    ),
                    external_event_type=message.event_type,
                )
                return _event_record(existing)
            row = ChatEventModel(
                bot_user_id=message.bot_user_id,
                platform_message_id=message.message_id,
                scope_type=message.scope_type.value,
                group_id=message.group_id,
                private_peer_user_id=message.sender.user_id,
                sender_user_id=message.sender.user_id,
                sender_nickname=message.sender.nickname,
                sender_group_card=message.sender.group_card,
                direction="inbound",
                event_kind="message",
                content=message.text,
                visual_summary="",
                reply_to_message_id=message.reply_to_message_id,
                segments_json=json.dumps(segments, ensure_ascii=False, separators=(",", ":")),
                origin="user_message",
                occurred_at=message.occurred_at or message.received_at,
                observed_at=now,
                canonical_event_id=canonical_id,
                canonical_conversation_id=None,
                author_kind=AuthorKind.ACCOUNT.value,
                author_account_id=admitted.author_account_id,
                author_person_id=None,
                author_presence_id=None,
                ingress_presence_id=admitted.presence_id,
                suppression_status="keeper",
                utterance_fingerprint=_fingerprint(message.text),
                ingress_provider=admitted.provider,
                ingress_gateway_instance_id=admitted.gateway_instance_id,
            )
            presence = await session.get(PresenceModel, admitted.presence_id)
            assert presence is not None
            session.add_all(
                (
                    row,
                    CanonicalEventReceiptModel(
                        ingress_presence_id=admitted.presence_id,
                        ingress_account_id=presence.account_id,
                        transport_namespace=transport_namespace(scope),
                        event_type=message.event_type,
                        platform_message_id=message.message_id,
                        canonical_event_id=canonical_id,
                        created_at=now,
                        observed_at=now,
                    ),
                )
            )
            await session.flush()
            return _event_record(row)

        return await self._database.apply_snapshot_write(record)

    async def append_inbound(
        self,
        message: InboundMessage,
        admitted: IngressPreAdmit,
        *,
        new_generation: bool = False,
        session: AsyncSession | None = None,
        origin: str = "user_message",
        prepared_terms: tuple[str, ...] | None = None,
    ) -> ScopedAppendResult:
        if admitted.dropped or admitted.presence_id is None or admitted.conversation_id is None:
            raise CanonicalIdentityError("unclassified")
        if not admitted.provider or not admitted.handle_external_account_id:
            raise CanonicalIdentityError("unclassified")
        if message.bot_user_id and message.bot_user_id != admitted.handle_external_account_id:
            raise CanonicalIdentityError("bot_handle_mismatch")
        presence_id = admitted.presence_id
        conversation_id = admitted.conversation_id
        scope = admitted.message.scope(bot_user_id=admitted.handle_external_account_id)
        segments = list(snapshot_mention_segments(message.segments, message.mentions))
        segments.append(
            {
                "type": "yuki_context",
                "data": {
                    "platform": message.platform,
                    "thread_id": message.thread_id,
                    "native_occurred_at": message.occurred_at.isoformat()
                    if message.occurred_at
                    else None,
                    "source_assignment_id": admitted.author_assignment_id,
                    "mentioned_user_ids": list(message.mentioned_user_ids),
                    "mentioned_person_ids": list(message.mentioned_person_ids),
                    "reply_sender_user_id": message.reply_sender_user_id,
                },
            }
        )
        now = _utcnow()
        segments_json = json.dumps(segments, ensure_ascii=False, separators=(",", ":"))
        normalized_segments = normalize_live_json(segments_json)
        observed_reply = message.reply_segments if message.reply_to_event_id is None else ()
        reply_snapshots = reply_media_snapshots(observed_reply)
        utterance_fingerprint = _fingerprint(message.text)
        if message.scope_type is ScopeType.GROUP:
            if admitted.space_binding_id is None:
                raise CanonicalIdentityError("unclassified")
            # The accepted socket may close while this event waits for SQLite.
            # Its original ingest route is still checked in the transaction.
            if admitted.connection_id is None or admitted.gateway_instance_id is None:
                raise CanonicalIdentityError("unclassified")
        canonical_event_id = _new_id()
        if prepared_terms is None:
            prepared_terms = await asyncio.to_thread(prepare_terms, message.text)

        async def append(session: AsyncSession) -> ScopedAppendResult:
            observed_message, apply_accounts = await prepare_message_account_observations(
                session,
                message,
                space_binding_id=admitted.space_binding_id,
                now=now,
                assignment_id=admitted.author_assignment_id,
            )
            observed_segments = list(
                snapshot_mention_segments(observed_message.segments, observed_message.mentions)
            )
            observed_segments.append(segments[-1])
            stored_segments_json = json.dumps(
                observed_segments, ensure_ascii=False, separators=(",", ":")
            )
            # A rolled-back SQLite write must not retain its allocated integer PK.
            # The original canonical identity, payload and receipt key stay fixed.
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
                segments_json=stored_segments_json,
                reply_to_message_id=message.reply_to_message_id,
                reply_to_event_id=message.reply_to_event_id,
                origin=origin,
                occurred_at=message.occurred_at or message.received_at,
                observed_at=now,
                canonical_event_id=canonical_event_id,
                canonical_conversation_id=conversation_id,
                author_kind=admitted.author_kind,
                author_person_id=admitted.author_person_id,
                author_presence_id=admitted.author_presence_id,
                author_account_id=admitted.author_account_id,
                ingress_presence_id=presence_id,
                utterance_fingerprint=utterance_fingerprint,
                suppression_status="keeper",
                ingress_provider=admitted.provider,
                ingress_gateway_instance_id=admitted.gateway_instance_id,
            )
            _validate_author_shape(row)
            existing = await _existing_claimed_event(
                session,
                scope=scope,
                presence_id=presence_id,
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
                    normalized_segments=normalized_segments,
                )
            trip("before_fence_recheck")
            if message.scope_type is ScopeType.GROUP:
                assert admitted.space_binding_id is not None
                fence = await self._router.ingest_status_in_session(
                    session,
                    space_binding_id=admitted.space_binding_id,
                    event_presence_id=presence_id,
                    require_connected=False,
                )
                if fence != "ok":
                    raise CanonicalIdentityError(fence)
            trip("after_fence_recheck")
            if (
                new_generation
                and message.scope_type is ScopeType.GROUP
                and not await person_is_superuser(session, admitted.author_person_id)
            ):
                raise CanonicalIdentityError("capability_denied")
            if message.platform in LOCAL_CLIENT_PLATFORMS and admitted.author_person_id is not None:
                from yuki.client_api.requests import personal_command

                request_id = message.message_id.rsplit(":", 1)[-1]
                if (
                    await personal_command(
                        session,
                        admitted.author_person_id,
                        request_id,
                        conversation_id=(
                            admitted.conversation_id if message.platform == "acp" else None
                        ),
                    )
                    is not None
                ):
                    raise CanonicalIdentityError("receipt_conflict")
            await require_reply_source(
                session,
                conversation_id=conversation_id,
                reply_to_event_id=message.reply_to_event_id,
            )
            conversation = await session.get(CanonicalConversationModel, conversation_id)
            if conversation is None:
                raise CanonicalIdentityError("unclassified")
            ingress_presence = await session.get(PresenceModel, presence_id)
            if ingress_presence is None:
                raise CanonicalIdentityError("unclassified")
            # All owner, alias and membership preparation belongs to this read
            # snapshot. Only the successful original receipt applies the update.
            observation = await _prepare_observation(session, message, admitted, _utcnow())
            try:
                async with session.begin_nested():
                    session.add(
                        CanonicalEventReceiptModel(
                            ingress_presence_id=presence_id,
                            ingress_account_id=ingress_presence.account_id,
                            transport_namespace=transport_namespace(scope),
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
                    scope=scope,
                    presence_id=presence_id,
                    event_type=message.event_type[:64],
                    platform_message_id=message.message_id[:128],
                )
                if raced is None:
                    raise CanonicalIdentityError("receipt_conflict") from exc
                return await _reuse_claimed_inbound(
                    session,
                    raced,
                    message=message,
                    admitted=admitted,
                    scope=scope,
                    segments=segments,
                    normalized_segments=normalized_segments,
                )
            trip("after_receipt_claim")
            apply_accounts()
            if observation is not None:
                observation()
            session.add(row)
            await session.flush()
            await replace_event(
                session,
                row.id,
                conversation_id,
                prepared_terms,
                author_person_id=row.author_person_id,
                author_kind=row.author_kind,
            )
            event = _event_record(row)
            await touch_canonical_watermarks(
                session,
                conversation_id,
                event_id=row.id,
            )
            if new_generation:
                await bump_canonical_generation(
                    session,
                    conversation_id,
                    event_id=row.id,
                )
            if not new_generation:
                from yuki.conversation.media_index import index_event_media

                await index_event_media(
                    session,
                    event_id=row.id,
                    conversation_id=conversation_id,
                    generation=int(conversation.generation),
                    segments=segments,
                    reply_segments=observed_reply,
                    reply_snapshots=reply_snapshots,
                )
            trip("after_canonical_append")
            state = await hydrate_scope_state_from_canonical(session, scope, conversation)
            return ScopedAppendResult(event=event, scope=state, created=True)

        if session is not None:
            return await append(session)
        return await self._database.apply_snapshot_write(append)

    async def append_new_generation(
        self,
        message: InboundMessage,
        admitted: IngressPreAdmit,
    ) -> NewGenerationResult:
        appended = await self.append_inbound(message, admitted, new_generation=True)
        return NewGenerationResult(
            event=appended.event,
            scope=appended.scope,
            generation_changed=(
                appended.created
                and appended.scope.last_generation_change_event_id == appended.event.id
            ),
        )


async def _prepare_observation(
    session: AsyncSession,
    message: InboundMessage,
    admitted: IngressPreAdmit,
    now: datetime,
) -> Callable[[], None] | None:
    if admitted.author_kind == AuthorKind.PERSON.value:
        binding = (
            await session.get(IdentityBindingModel, admitted.author_assignment_id)
            if admitted.author_assignment_id is not None
            else await find_identity_binding(
                session, message.sender.user_id, platform=message.platform
            )
        )
        if binding is None or binding.person_id != admitted.author_person_id:
            raise CanonicalIdentityError("canonical_owner_mismatch")
        return await prepare_canonical_person_observation(
            session,
            binding=binding,
            nickname=sanitize_profile_name(message.sender.nickname),
            group_id=message.group_id,
            group_card=sanitize_profile_name(message.sender.group_card),
            group_name=sanitize_profile_name(message.group_name),
            nickname_known=message.sender.nickname_known or bool(message.sender.nickname),
            group_card_known=message.sender.group_card_known or bool(message.sender.group_card),
            now=now,
            space_id=admitted.space_id,
        )
    if admitted.space_binding_id is not None and admitted.space_id is not None:
        space_binding = await session.get(SpaceBindingModel, admitted.space_binding_id)
        space = await session.get(CanonicalSpaceModel, admitted.space_id)
        if space_binding is None or space is None:
            raise CanonicalIdentityError("unclassified")
        return partial(
            apply_canonical_space_observation,
            space,
            space_binding,
            name=sanitize_profile_name(message.group_name),
            now=now,
        )
    return None


async def _load_receipt(
    session: AsyncSession,
    *,
    presence_id: str,
    scope: ConversationScope,
    event_type: str,
    platform_message_id: str,
) -> CanonicalEventReceiptModel | None:
    return await transport_receipt(
        session,
        presence_id=presence_id,
        scope=scope,
        event_type=event_type,
        platform_message_id=platform_message_id,
    )


async def _existing_claimed_event(
    session: AsyncSession,
    *,
    presence_id: str,
    scope: ConversationScope,
    event_type: str,
    platform_message_id: str,
) -> ChatEventModel | None:
    receipt = await _load_receipt(
        session,
        scope=scope,
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
    normalized_segments: str,
) -> ScopedAppendResult:
    if admitted.conversation_id is None or admitted.presence_id is None:
        raise CanonicalIdentityError("unclassified")
    conversation = await session.get(CanonicalConversationModel, existing.canonical_conversation_id)
    if conversation is None:
        raise CanonicalIdentityError("unclassified")
    receipt = await _load_receipt(
        session,
        scope=scope,
        presence_id=admitted.presence_id,
        event_type=message.event_type[:64],
        platform_message_id=message.message_id[:128],
    )
    require_claimed_event(receipt, existing)
    if conversation.kind == "private":
        original_binding = await session.get(IdentityBindingModel, conversation.identity_binding_id)
        if (
            original_binding is None
            or original_binding.external_account_id != existing.private_peer_user_id
            or original_binding.platform != message.platform
        ):
            raise CanonicalIdentityError("receipt_conflict")
    content = message.text
    original_segments = json.loads(existing.segments_json)

    if (
        existing.reply_to_message_id != message.reply_to_message_id
        or conversation.thread_id != message.thread_id
    ):
        raise CanonicalIdentityError("receipt_conflict")
    if _native_payload(original_segments, existing.content) == _native_payload(segments, content):
        # The original receipt owns the accepted author and display snapshot,
        # even after account retirement, transfer or a profile observation.
        segments = original_segments
        normalized_segments = normalize_live_json(existing.segments_json)
        content = existing.content
    scope = replace(message, conversation_id=conversation.id).scope(
        bot_user_id=admitted.handle_external_account_id
    )
    require_compatible_v2_live(
        existing,
        scope=scope,
        conversation_id=conversation.id,
        presence_id=admitted.presence_id,
        platform_message_id=message.message_id,
        sender_user_id=message.sender.user_id,
        direction="inbound",
        event_kind="message",
        content=content,
        segments=segments,
        normalized_segments=normalized_segments,
        timestamp=message.occurred_at or existing.occurred_at,
        author_kind=existing.author_kind,
        author_person_id=existing.author_person_id,
        author_presence_id=existing.author_presence_id,
        receipt=receipt,
        external_event_type=message.event_type,
    )
    return ScopedAppendResult(
        event=_event_record(existing),
        scope=await hydrate_scope_state_from_canonical(session, scope, conversation),
        created=False,
    )


def _native_payload(parts: list[dict[str, object]], text: str) -> tuple[str, str]:
    native: list[object] = []
    body: list[str] = []
    cursor = 0
    for part in parts:
        if part.get("type") == "yuki_context":
            continue
        data = part.get("data")
        if part.get("type") == "mention" and isinstance(data, dict) and "source" in data:
            native.append(data["source"])
            body.extend((text[cursor : data["text_start"]], "@"))
            cursor = data["text_end"]
        else:
            native.append(part)
    body.append(text[cursor:])
    return normalize_live_json(native), normalize_live_text("".join(body))


def _validate_author_shape(row: ChatEventModel) -> None:
    kind = row.author_kind
    if kind == AuthorKind.ACCOUNT.value:
        if (
            row.author_account_id is None
            or row.author_person_id is not None
            or row.author_presence_id is not None
        ):
            raise CanonicalIdentityError("unclassified")
        return
    if kind == AuthorKind.PERSON.value:
        if row.author_person_id is None or row.author_presence_id is not None:
            raise CanonicalIdentityError("unclassified")
        return
    if kind == AuthorKind.YUKI.value:
        if row.author_presence_id is None or row.author_person_id is not None:
            raise CanonicalIdentityError("unclassified")
        return
    if kind in {AuthorKind.EXTERNAL_BOT.value, AuthorKind.SYSTEM.value}:
        if row.author_person_id is not None or row.author_presence_id is not None:
            raise CanonicalIdentityError("unclassified")
        return
    raise CanonicalIdentityError("unclassified")
