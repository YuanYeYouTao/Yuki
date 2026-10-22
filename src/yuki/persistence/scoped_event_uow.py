"""Confirmed delivery and external facts; native input uses canonical ingress."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from functools import partial
from typing import Any, Literal
from uuid import uuid4

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from yuki.conversation.canonical_db_models import (
    CanonicalConversationModel,
    CanonicalEventReceiptModel,
)
from yuki.conversation.hydrate import (
    HydratedConversation,
    ensure_canonical_conversation,
    hydrate_scope_state_from_canonical,
    require_primary_alias_for_conversation,
    touch_canonical_watermarks,
)
from yuki.conversation.initiative_sources import source_revision
from yuki.conversation.media_index import prepare_event_media
from yuki.conversation.scope_models import ConversationScopeState
from yuki.domain.conversations import ConversationScope, ScopeType
from yuki.domain.identity import AuthorKind
from yuki.domain.messages import InboundMessage, snapshot_mention_segments
from yuki.identity.canonical_repository import (
    trip,
)
from yuki.identity.db_models import (
    CanonicalPersonModel,
    IdentityBindingModel,
    PresenceModel,
    SpaceBindingModel,
)
from yuki.identity.errors import CanonicalIdentityError
from yuki.identity.mentions import resolve_message_mentions
from yuki.identity.receipt_compat import (
    load_claimed_keeper,
    normalize_live_json,
    require_claimed_event,
    require_compatible_v2_live,
    require_reply_source,
    transport_namespace,
    transport_receipt,
)
from yuki.persistence.database import Database
from yuki.persistence.history_search import prepare_terms
from yuki.persistence.history_search_index import replace_event
from yuki.persistence.models import ChatEventModel, ConversationMediaItemModel
from yuki.persistence.repository_helpers import _event_record
from yuki.persistence.repository_records import EventRecord


@dataclass(frozen=True, slots=True)
class ScopedEventPreparation:
    terms: tuple[str, ...]
    canonical_event_id: str
    segments_json: str
    external_payload_json: str | None


async def prepare_append(
    content: str,
    *,
    segments: tuple[dict[str, Any], ...] = (),
    external_payload: dict[str, Any] | None = None,
    prepared_terms: tuple[str, ...] | None = None,
) -> ScopedEventPreparation:
    return ScopedEventPreparation(
        canonical_event_id=str(uuid4()),
        terms=prepared_terms
        if prepared_terms is not None
        else await asyncio.to_thread(prepare_terms, content),
        segments_json=json.dumps(segments, ensure_ascii=False, separators=(",", ":")),
        external_payload_json=json.dumps(
            external_payload, ensure_ascii=False, separators=(",", ":")
        )
        if external_payload is not None
        else None,
    )


@dataclass(frozen=True, slots=True)
class ScopedAppendResult:
    event: EventRecord
    scope: ConversationScopeState
    created: bool


@dataclass(frozen=True, slots=True)
class NewGenerationResult:
    event: EventRecord
    scope: ConversationScopeState
    generation_changed: bool


@dataclass(frozen=True, slots=True)
class _DerivedTextUpdate:
    event: EventRecord
    source_token: tuple[Any, ...] | None


def _derived_source_token(conversation: CanonicalConversationModel) -> tuple[Any, ...]:
    return (
        conversation.generation,
        conversation.prompt_source_revision,
        conversation.starts_after_event_id,
        conversation.last_event_id,
        conversation.revision,
        conversation.kind,
        conversation.person_id,
        conversation.space_id,
    )


class ScopedEventLedgerUnitOfWork:
    """Commit source events and canonical identity watermarks atomically."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def append_external(
        self,
        *,
        scope: ConversationScope,
        platform_message_id: str,
        source_plugin_id: str,
        external_source: str,
        external_event_key: str,
        external_event_type: str,
        external_payload: dict[str, Any],
        external_target_id: str,
        content: str,
        occurred_at: datetime,
        session: AsyncSession | None = None,
        prepared_terms: tuple[str, ...] | None = None,
        prepared: ScopedEventPreparation | None = None,
    ) -> ScopedAppendResult:
        """Persist one plugin/automation event through the scoped ledger boundary."""

        return await self.append(
            scope=scope,
            platform_message_id=platform_message_id,
            sender_user_id=scope.bot_user_id,
            direction="external",
            content=content,
            occurred_at=occurred_at,
            origin="plugin_background",
            event_kind="external_event",
            source_plugin_id=source_plugin_id,
            external_source=external_source,
            external_event_key=external_event_key,
            external_event_type=external_event_type,
            external_payload=external_payload,
            external_target_id=external_target_id,
            session=session,
            prepared_terms=prepared_terms,
            prepared=prepared,
        )

    async def append(
        self,
        *,
        scope: ConversationScope,
        platform_message_id: str,
        sender_user_id: str,
        direction: Literal["outbound", "external"],
        content: str,
        segments: tuple[dict[str, Any], ...] = (),
        reply_to_message_id: str | None = None,
        reply_to_event_id: int | None = None,
        occurred_at: datetime | None = None,
        sender_nickname: str = "",
        sender_group_card: str = "",
        origin: str = "user_message",
        automation_id: int | None = None,
        automation_run_id: int | None = None,
        event_kind: str = "message",
        source_plugin_id: str | None = None,
        external_source: str | None = None,
        external_event_key: str | None = None,
        external_event_type: str | None = None,
        external_payload: dict[str, Any] | None = None,
        external_target_id: str | None = None,
        caused_by_event_id: int | None = None,
        session: AsyncSession | None = None,
        identity_binding_id: str | None = None,
        space_binding_id: str | None = None,
        source_event_id: int | None = None,
        prepared_terms: tuple[str, ...] | None = None,
        prepared: ScopedEventPreparation | None = None,
    ) -> ScopedAppendResult:
        """Record confirmed outbound or external facts using their original entry."""
        timestamp = occurred_at
        observed_at = datetime.now(UTC)
        if prepared is None:
            if session is not None:
                raise ValueError("scoped_event_preparation_required")
            prepared = await prepare_append(
                content,
                segments=segments,
                external_payload=external_payload,
                prepared_terms=prepared_terms,
            )
        append = partial(
            self._append_canonical,
            scope=scope,
            platform_message_id=platform_message_id,
            sender_user_id=sender_user_id,
            direction=direction,
            content=content,
            segments=segments,
            reply_to_message_id=reply_to_message_id,
            reply_to_event_id=reply_to_event_id,
            timestamp=timestamp,
            observed_at=observed_at,
            sender_nickname=sender_nickname,
            sender_group_card=sender_group_card,
            origin=origin,
            automation_id=automation_id,
            automation_run_id=automation_run_id,
            event_kind=event_kind,
            source_plugin_id=source_plugin_id,
            external_source=external_source,
            external_event_key=external_event_key,
            external_event_type=external_event_type,
            external_payload=external_payload,
            external_target_id=external_target_id,
            caused_by_event_id=caused_by_event_id,
            identity_binding_id=identity_binding_id,
            space_binding_id=space_binding_id,
            source_event_id=source_event_id,
            prepared_terms=prepared.terms,
            prepared_segments_json=prepared.segments_json,
            prepared_external_json=prepared.external_payload_json,
            canonical_event_id=prepared.canonical_event_id,
        )
        if session is not None:
            result = await append(session)
            assert result is not None
            return result
        if scope.conversation_id is not None and scope.presence_id is not None:
            async with self._database.read_session() as reader:
                await reader.execute(text("BEGIN"))
                existing, _ = await self._find_existing_live(
                    reader,
                    scope=scope,
                    presence_id=scope.presence_id,
                    platform_message_id=platform_message_id,
                    event_type=(external_event_type or "message")[:64],
                    source_plugin_id=source_plugin_id,
                    external_event_key=external_event_key,
                    external_target_id=external_target_id,
                    event_kind=event_kind,
                )
                if existing is not None:
                    result = await append(reader, read_only=True)
                    assert result is not None
                    return result
        async with self._database.immediate_session() as writer:
            result = await append(writer)
            assert result is not None
            return result

    async def set_visual_summary(self, event_id: int, summary: str) -> bool:
        normalized = summary.strip()[:6000]
        lowered = normalized.casefold()
        if "data:image/" in lowered or "base64://" in lowered:
            raise ValueError("visual_summary must not contain image or Base64 payloads")
        return await self._set_derived_text(event_id, normalized, audio=False)

    async def update_platform_message(
        self,
        event_id: int,
        message: InboundMessage,
        *,
        presence_id: str,
        edited_at: datetime,
        revision: int,
    ) -> None:
        """Apply an authenticated edit to its original event, never a new turn."""
        while True:
            async with self._database.read_session() as reader:
                original = await reader.get(ChatEventModel, event_id)
                if (
                    original is None
                    or original.platform_message_id != message.message_id
                    or original.sender_user_id != message.sender.user_id
                    or original.direction != "inbound"
                ):
                    raise CanonicalIdentityError("receipt_conflict")
                source_segments, source_content = original.segments_json, original.content
                conversation_id = original.canonical_conversation_id
                conversation = (
                    await reader.get(CanonicalConversationModel, conversation_id)
                    if conversation_id
                    else None
                )
                presence = await reader.get(PresenceModel, presence_id)
                original_presence = await reader.get(PresenceModel, original.ingress_presence_id)
                if (
                    presence is None
                    or original_presence is None
                    or original_presence.account_id != presence.account_id
                    or presence.platform != message.platform
                ):
                    raise CanonicalIdentityError("receipt_conflict")
                generation = conversation.generation if conversation else 0
                prepared_message = await resolve_message_mentions(
                    reader,
                    replace(
                        message,
                        conversation_id=conversation_id,
                        reply_to_event_id=original.reply_to_event_id,
                    ),
                    space_id=conversation.space_id if conversation else None,
                )
                prepared_segments = snapshot_mention_segments(
                    prepared_message.segments, prepared_message.mentions
                )
                original_at = original.occurred_at.replace(tzinfo=UTC).timestamp()
            # Parse/prepare outside the SQLite writer, preserving Host identity metadata.
            decoded = json.loads(source_segments)
            metadata = [part for part in decoded if part.get("type") == "yuki_context"]
            context = metadata[0]["data"] if metadata else {}
            if (
                float(context.get("platform_edit_date", original_at)),
                int(context.get("platform_update_id", -1)),
            ) >= (edited_at.timestamp(), revision):
                return
            context.update(
                mentioned_user_ids=list(message.mentioned_user_ids),
                mentioned_person_ids=list(
                    dict.fromkeys(
                        item.person_id for item in prepared_message.mentions if item.person_id
                    )
                ),
                reply_sender_user_id=message.reply_sender_user_id,
                platform_edit_date=edited_at.timestamp(),
            )
            context["platform_update_id"] = revision
            if not metadata:
                metadata.append({"type": "yuki_context", "data": context})
            segments = json.dumps(
                [*prepared_segments, *metadata], ensure_ascii=False, separators=(",", ":")
            )
            media = (
                prepare_event_media(
                    event_id=event_id,
                    conversation_id=conversation_id,
                    generation=generation,
                    segments=prepared_segments,
                    reply_segments=prepared_message.reply_segments
                    if prepared_message.reply_to_event_id is None
                    else (),
                )
                if conversation_id is not None
                else ()
            )
            prepared_terms = await asyncio.to_thread(prepare_terms, prepared_message.text)
            async with self._database.immediate_session() as session:
                event = await session.get(ChatEventModel, event_id)
                if event is None:
                    raise CanonicalIdentityError("receipt_conflict")
                if event.segments_json != source_segments or event.content != source_content:
                    continue
                conversation = (
                    await session.get(CanonicalConversationModel, conversation_id)
                    if conversation_id
                    else None
                )
                if conversation is not None and conversation.generation != generation:
                    continue
                current_media = {
                    item.attachment_index: item
                    for item in await session.scalars(
                        select(ConversationMediaItemModel).where(
                            ConversationMediaItemModel.source_event_id == event_id
                        )
                    )
                }
                for prepared in media:
                    current = current_media.pop(prepared.attachment_index, None)
                    if current is None:
                        session.add(prepared)
                        continue
                    if current.source_snapshot_json != prepared.source_snapshot_json or (
                        current.source_snapshot_json is None
                        and decoded[current.segment_index]
                        != prepared_segments[prepared.segment_index]
                    ):
                        current.cache_status = "uncached"
                        current.content_sha256 = current.cache_name = None
                        current.cached_at = current.expires_at = None
                        current.created_at = prepared.created_at
                    current.segment_index = prepared.segment_index
                    current.kind = prepared.kind
                    current.source_snapshot_json = prepared.source_snapshot_json
                    current.display_name = prepared.display_name
                    current.declared_size = prepared.declared_size
                for removed in current_media.values():
                    await session.delete(removed)
                event.content, event.segments_json = prepared_message.text, segments
                event.visual_summary, event.audio_transcript = "", ""
                await session.flush()
                await replace_event(
                    session,
                    event.id,
                    conversation_id,
                    prepared_terms,
                    author_person_id=event.author_person_id,
                    author_kind=event.author_kind,
                )
                if conversation is not None:
                    conversation.prompt_source_revision += 1
                    conversation.revision += 1
            return

    async def set_audio_transcript(
        self,
        event_id: int,
        transcript: str,
        *,
        generation: int,
        expected_source: EventRecord | None = None,
    ) -> bool:
        """Save one bounded result only while its source generation is still live."""
        normalized = transcript.strip()
        from yuki.domain.audio import parse_transcripts

        if not parse_transcripts(normalized):
            raise ValueError("audio transcript must not be empty")
        return await self._set_derived_text(
            event_id, normalized, audio=True, generation=generation, expected_source=expected_source
        )

    async def _prepare_derived_text(
        self,
        event_id: int,
        normalized: str,
        *,
        audio: bool,
        generation: int | None,
        expected_source: EventRecord | None = None,
    ) -> _DerivedTextUpdate | bool:
        # Keep the conditional history scan and all rendering outside the writer.
        async with self._database.read_session() as reader:
            await reader.execute(text("BEGIN"))
            row = await reader.get(ChatEventModel, event_id)
            if row is None:
                return False
            if expected_source is not None and (
                source_revision(_event_record(row)) != source_revision(expected_source)
                or _event_record(row).segments != expected_source.segments
            ):
                return False
            conversation = (
                await reader.get(CanonicalConversationModel, row.canonical_conversation_id)
                if row.canonical_conversation_id
                else None
            )
            if audio:
                if (
                    conversation is None
                    or conversation.generation != generation
                    or row.id <= conversation.starts_after_event_id
                ):
                    return False
                if row.audio_transcript:
                    return row.audio_transcript == normalized
            return _DerivedTextUpdate(
                _event_record(row),
                _derived_source_token(conversation) if conversation is not None else None,
            )

    async def _set_derived_text(
        self,
        event_id: int,
        normalized: str,
        *,
        audio: bool,
        generation: int | None = None,
        expected_source: EventRecord | None = None,
    ) -> bool:
        while True:
            prepared = await self._prepare_derived_text(
                event_id,
                normalized,
                audio=audio,
                generation=generation,
                expected_source=expected_source,
            )
            if isinstance(prepared, bool):
                return prepared
            prepared_terms = (
                await asyncio.to_thread(prepare_terms, prepared.event.content, normalized)
                if audio
                else ()
            )
            async with self._database.immediate_session() as session:
                row = await session.get(ChatEventModel, event_id)
                if row is None:
                    return False
                conversation = (
                    await session.get(CanonicalConversationModel, row.canonical_conversation_id)
                    if row.canonical_conversation_id
                    else None
                )
                if audio and (
                    conversation is None
                    or conversation.generation != generation
                    or row.id <= conversation.starts_after_event_id
                ):
                    return False
                if audio and row.audio_transcript:
                    return row.audio_transcript == normalized
                if (
                    _event_record(row) != prepared.event
                    or (_derived_source_token(conversation) if conversation is not None else None)
                    != prepared.source_token
                ):
                    continue
                # The writer has validated the exact snapshot; only bounded PK
                # writes remain. No source scan or rendering occurs after flush.
                if audio:
                    row.audio_transcript = normalized
                else:
                    row.visual_summary = normalized
                await session.flush()
                if audio:
                    await replace_event(
                        session,
                        row.id,
                        row.canonical_conversation_id,
                        prepared_terms,
                        author_person_id=row.author_person_id,
                        author_kind=row.author_kind,
                    )
            return True

    async def _find_existing_live(
        self,
        session: AsyncSession,
        *,
        scope: ConversationScope,
        presence_id: str,
        platform_message_id: str,
        event_type: str,
        source_plugin_id: str | None = None,
        external_event_key: str | None = None,
        external_target_id: str | None = None,
        event_kind: str = "message",
    ) -> tuple[ChatEventModel | None, CanonicalEventReceiptModel | None]:
        receipt = await transport_receipt(
            session,
            presence_id=presence_id,
            scope=scope,
            event_type=event_type,
            platform_message_id=platform_message_id,
        )
        if receipt is not None:
            claimed = await load_claimed_keeper(session, receipt.canonical_event_id)
            return require_claimed_event(receipt, claimed), receipt
        plugin_external = (
            event_kind == "external_event" or bool(source_plugin_id) or bool(external_event_key)
        )
        if plugin_external:
            return await self._find_existing_plugin_external(
                session,
                scope=scope,
                source_plugin_id=source_plugin_id,
                external_event_key=external_event_key,
                external_target_id=external_target_id,
            ), None
        # Canonical-only schemas require a non-null event ID for every ledger
        # row. A receipt miss cannot be rescued by a legacy NULL row.
        return None, None

    async def _find_existing_plugin_external(
        self,
        session: AsyncSession,
        *,
        scope: ConversationScope,
        source_plugin_id: str | None,
        external_event_key: str | None,
        external_target_id: str | None,
    ) -> ChatEventModel | None:
        """Reuse by the unique external key. Never invent a receipt or pick first-row."""

        if not source_plugin_id or not external_event_key or not external_target_id:
            raise CanonicalIdentityError("receipt_conflict")
        rows = list(
            (
                await session.scalars(
                    select(ChatEventModel).where(
                        ChatEventModel.event_kind == "external_event",
                        ChatEventModel.source_plugin_id == source_plugin_id,
                        ChatEventModel.external_event_key == external_event_key,
                        ChatEventModel.canonical_conversation_id == scope.conversation_id,
                        ChatEventModel.scope_type == scope.scope_type.value,
                        ChatEventModel.external_target_id == external_target_id,
                    )
                )
            ).all()
        )
        if len(rows) > 1:
            raise CanonicalIdentityError("receipt_conflict")
        return rows[0] if rows else None

    async def _load_receipt_claimed_event(
        self,
        session: AsyncSession,
        *,
        scope: ConversationScope,
        presence_id: str,
        event_type: str,
        platform_message_id: str,
    ) -> tuple[CanonicalEventReceiptModel | None, ChatEventModel | None]:
        receipt = await transport_receipt(
            session,
            presence_id=presence_id,
            scope=scope,
            event_type=event_type,
            platform_message_id=platform_message_id,
        )
        if receipt is None:
            return None, None
        claimed = await load_claimed_keeper(session, receipt.canonical_event_id)
        return receipt, require_claimed_event(receipt, claimed)

    async def _require_causal_source(
        self,
        session: AsyncSession,
        *,
        conversation_id: str,
        caused_by_event_id: int | None,
        direction: Literal["outbound", "external"],
        event_kind: str,
        origin: str,
    ) -> None:
        """Validate durable causality before appending a proactive plugin reply."""

        plugin_outbound = (
            origin == "plugin_background" and direction == "outbound" and event_kind == "message"
        )
        if plugin_outbound and caused_by_event_id is None:
            raise CanonicalIdentityError("causal_source_required")
        if caused_by_event_id is None:
            return
        task_outbound = (
            origin == "system_task" and direction == "outbound" and event_kind == "message"
        )
        if not plugin_outbound and not task_outbound:
            raise CanonicalIdentityError("causal_source_not_allowed")
        source = await session.get(ChatEventModel, caused_by_event_id)
        if (
            source is None
            or source.canonical_conversation_id != conversation_id
            or (
                plugin_outbound
                and (
                    source.event_kind != "external_event"
                    or source.direction != "external"
                    or source.origin != "plugin_background"
                )
            )
            or (task_outbound and (source.event_kind != "message" or source.direction != "inbound"))
            or source.suppression_status != "keeper"
        ):
            raise CanonicalIdentityError("causal_source_invalid")

    def _require_compatible_live(
        self,
        existing: ChatEventModel,
        *,
        scope: ConversationScope,
        conversation_id: str,
        presence_id: str,
        platform_message_id: str,
        sender_user_id: str,
        direction: Literal["outbound", "external"],
        event_kind: str,
        content: str,
        segments: tuple[dict[str, Any], ...],
        timestamp: datetime,
        author_kind: str,
        author_person_id: str | None,
        author_presence_id: str | None,
        receipt: CanonicalEventReceiptModel | None,
        external_event_type: str | None,
        source_plugin_id: str | None = None,
        external_event_key: str | None = None,
        external_target_id: str | None = None,
        external_payload: dict[str, Any] | None = None,
        caused_by_event_id: int | None = None,
    ) -> None:
        require_compatible_v2_live(
            existing,
            scope=scope,
            conversation_id=conversation_id,
            presence_id=presence_id,
            platform_message_id=platform_message_id,
            sender_user_id=sender_user_id,
            direction=direction,
            event_kind=event_kind,
            content=content,
            segments=segments,
            timestamp=timestamp,
            author_kind=author_kind,
            author_person_id=author_person_id,
            author_presence_id=author_presence_id,
            receipt=receipt,
            external_event_type=external_event_type,
        )
        if existing.caused_by_event_id != caused_by_event_id:
            raise CanonicalIdentityError("receipt_conflict")
        if event_kind != "external_event":
            return
        incoming_payload = (
            json.dumps(external_payload, ensure_ascii=False, separators=(",", ":"))
            if external_payload is not None
            else None
        )
        if (
            existing.source_plugin_id != source_plugin_id
            or existing.external_event_key != external_event_key
            or existing.external_target_id != external_target_id
            or normalize_live_json(existing.external_payload_json)
            != normalize_live_json(incoming_payload)
        ):
            raise CanonicalIdentityError("receipt_conflict")

    async def _reuse_identical_live(
        self,
        session: AsyncSession,
        existing: ChatEventModel,
        *,
        scope: ConversationScope,
        conversation: CanonicalConversationModel,
        presence_id: str,
        platform_message_id: str,
        sender_user_id: str,
        direction: Literal["outbound", "external"],
        event_kind: str,
        content: str,
        segments: tuple[dict[str, Any], ...],
        timestamp: datetime | None,
        receipt: CanonicalEventReceiptModel | None,
        external_event_type: str | None,
        source_plugin_id: str | None = None,
        external_event_key: str | None = None,
        external_target_id: str | None = None,
        external_payload: dict[str, Any] | None = None,
        caused_by_event_id: int | None = None,
    ) -> ScopedAppendResult:
        self._require_compatible_live(
            existing,
            scope=scope,
            conversation_id=conversation.id,
            presence_id=presence_id,
            platform_message_id=platform_message_id,
            sender_user_id=sender_user_id,
            direction=direction,
            event_kind=event_kind,
            content=content,
            segments=segments,
            timestamp=timestamp or existing.occurred_at,
            author_kind=existing.author_kind,
            author_person_id=existing.author_person_id,
            author_presence_id=existing.author_presence_id,
            receipt=receipt,
            external_event_type=external_event_type,
            source_plugin_id=source_plugin_id,
            external_event_key=external_event_key,
            external_target_id=external_target_id,
            external_payload=external_payload,
            caused_by_event_id=caused_by_event_id,
        )
        return ScopedAppendResult(
            event=_event_record(existing),
            scope=await hydrate_scope_state_from_canonical(session, scope, conversation),
            created=False,
        )

    async def _append_canonical(
        self,
        session: AsyncSession,
        *,
        scope: ConversationScope,
        platform_message_id: str,
        sender_user_id: str,
        direction: Literal["outbound", "external"],
        content: str,
        segments: tuple[dict[str, Any], ...],
        reply_to_message_id: str | None,
        reply_to_event_id: int | None,
        timestamp: datetime | None,
        observed_at: datetime,
        sender_nickname: str,
        sender_group_card: str,
        origin: str,
        automation_id: int | None,
        automation_run_id: int | None,
        event_kind: str,
        source_plugin_id: str | None,
        external_source: str | None,
        external_event_key: str | None,
        external_event_type: str | None,
        external_payload: dict[str, Any] | None,
        external_target_id: str | None,
        caused_by_event_id: int | None,
        identity_binding_id: str | None = None,
        space_binding_id: str | None = None,
        source_event_id: int | None = None,
        prepared_terms: tuple[str, ...],
        prepared_segments_json: str,
        prepared_external_json: str | None,
        canonical_event_id: str,
        read_only: bool = False,
    ) -> ScopedAppendResult | None:
        from sqlalchemy.exc import IntegrityError

        presence = await session.get(PresenceModel, scope.presence_id)
        if presence is None:
            raise CanonicalIdentityError("no_presence")
        presence_id = presence.id
        if scope.conversation_id is not None:
            original = await session.get(CanonicalConversationModel, scope.conversation_id)
            if original is None:
                raise CanonicalIdentityError("no_conversation")
            if original.kind == "private":
                binding = await session.get(IdentityBindingModel, original.identity_binding_id)
                from yuki.conversation.event_entry import event_entry_matches

                original_entry = (
                    binding is not None
                    and direction == "outbound"
                    and await event_entry_matches(
                        session, original, binding, presence, source_event_id or caused_by_event_id
                    )
                )
                if (
                    scope.scope_type is not ScopeType.PRIVATE
                    or binding is None
                    or binding.person_id != original.person_id
                    or binding.platform != presence.platform
                    or binding.external_account_id != scope.private_peer_user_id
                    or (
                        original.entry_presence_id is not None
                        and original.entry_presence_id != presence.id
                        and not original_entry
                    )
                ):
                    raise CanonicalIdentityError("canonical_owner_mismatch")
            else:
                if scope.scope_type is not ScopeType.GROUP or scope.group_id is None:
                    raise CanonicalIdentityError("canonical_owner_mismatch")
                entry_space_binding = await session.get(
                    SpaceBindingModel, original.space_binding_id or space_binding_id
                )
                if (
                    scope.scope_type is not ScopeType.GROUP
                    or entry_space_binding is None
                    or entry_space_binding.space_id != original.space_id
                    or entry_space_binding.platform != presence.platform
                    or entry_space_binding.external_space_id != scope.group_id
                    or original.thread_id != scope.thread_id
                ):
                    raise CanonicalIdentityError("canonical_owner_mismatch")
            hydrated = HydratedConversation(
                original.id,
                original.kind,
                original.person_id,
                original.space_id,
                await require_primary_alias_for_conversation(session, original.id),
                original.generation,
            )
        else:
            if scope.scope_type is ScopeType.GROUP:
                if scope.group_id is None:
                    raise CanonicalIdentityError("no_space_binding")
                space_binding = await session.get(SpaceBindingModel, space_binding_id)
                if (
                    space_binding is None
                    or space_binding.platform != presence.platform
                    or space_binding.external_space_id != scope.group_id
                ):
                    raise CanonicalIdentityError("canonical_owner_mismatch")
                hydrated = await ensure_canonical_conversation(
                    session,
                    kind="space",
                    primary_scope_key=scope.key,
                    space_id=space_binding.space_id,
                    space_binding_id=space_binding.id,
                    thread_id=scope.thread_id,
                )
            else:
                person_binding = await session.get(IdentityBindingModel, identity_binding_id)
                if (
                    direction != "outbound"
                    or person_binding is None
                    or person_binding.platform != presence.platform
                    or person_binding.external_account_id != scope.private_peer_user_id
                ):
                    raise CanonicalIdentityError("canonical_owner_mismatch")
                person = (
                    None
                    if person_binding is None
                    else await session.get(CanonicalPersonModel, person_binding.person_id)
                )
                if person is None:
                    raise CanonicalIdentityError("no_person")
                assert person_binding is not None
                hydrated = await ensure_canonical_conversation(
                    session,
                    kind="private",
                    primary_scope_key=scope.key,
                    person_id=person.id,
                    identity_binding_id=person_binding.id,
                    presence_id=presence.id,
                    thread_id=scope.thread_id,
                )
        scope = replace(
            scope,
            conversation_id=hydrated.conversation_id,
            key=f"conversation:{hydrated.conversation_id}",
        )
        receipt_event_type = (external_event_type or "message")[:64]
        existing, receipt = await self._find_existing_live(
            session,
            scope=scope,
            presence_id=presence_id,
            platform_message_id=platform_message_id,
            event_type=receipt_event_type,
            source_plugin_id=source_plugin_id,
            external_event_key=external_event_key,
            external_target_id=external_target_id,
            event_kind=event_kind,
        )
        conversation = await session.get(CanonicalConversationModel, hydrated.conversation_id)
        if conversation is None:
            raise CanonicalIdentityError("unclassified")
        await self._require_causal_source(
            session,
            conversation_id=conversation.id,
            caused_by_event_id=caused_by_event_id,
            direction=direction,
            event_kind=event_kind,
            origin=origin,
        )
        if existing is not None:
            return await self._reuse_identical_live(
                session,
                existing,
                scope=scope,
                conversation=conversation,
                presence_id=presence_id,
                platform_message_id=platform_message_id,
                sender_user_id=sender_user_id,
                direction=direction,
                event_kind=event_kind,
                content=content,
                segments=segments,
                timestamp=timestamp,
                receipt=receipt,
                external_event_type=external_event_type,
                source_plugin_id=source_plugin_id,
                external_event_key=external_event_key,
                external_target_id=external_target_id,
                external_payload=external_payload,
                caused_by_event_id=caused_by_event_id,
            )
        if read_only:
            return None
        plugin_external = (
            event_kind == "external_event"
            or direction == "external"
            or bool(source_plugin_id)
            or bool(external_source)
            or bool(external_event_key)
        )
        if not plugin_external:
            try:
                async with session.begin_nested():
                    session.add(
                        CanonicalEventReceiptModel(
                            ingress_presence_id=presence_id,
                            ingress_account_id=presence.account_id,
                            transport_namespace=transport_namespace(scope),
                            event_type=receipt_event_type,
                            platform_message_id=platform_message_id[:128],
                            canonical_event_id=canonical_event_id,
                            created_at=observed_at,
                            observed_at=observed_at,
                        )
                    )
                    await session.flush()
            except IntegrityError as exc:
                raced_receipt, raced = await self._load_receipt_claimed_event(
                    session,
                    scope=scope,
                    presence_id=presence_id,
                    event_type=receipt_event_type,
                    platform_message_id=platform_message_id,
                )
                if raced_receipt is None or raced is None:
                    raise CanonicalIdentityError("receipt_conflict") from exc
                return await self._reuse_identical_live(
                    session,
                    raced,
                    scope=scope,
                    conversation=conversation,
                    presence_id=presence_id,
                    platform_message_id=platform_message_id,
                    sender_user_id=sender_user_id,
                    direction=direction,
                    event_kind=event_kind,
                    content=content,
                    segments=segments,
                    timestamp=timestamp,
                    receipt=raced_receipt,
                    external_event_type=external_event_type,
                    source_plugin_id=source_plugin_id,
                    external_event_key=external_event_key,
                    external_target_id=external_target_id,
                    external_payload=external_payload,
                    caused_by_event_id=caused_by_event_id,
                )
        await require_reply_source(
            session, conversation_id=conversation.id, reply_to_event_id=reply_to_event_id
        )
        row = ChatEventModel(
            bot_user_id=scope.bot_user_id,
            platform_message_id=platform_message_id,
            scope_type=scope.scope_type.value,
            group_id=scope.group_id,
            private_peer_user_id=scope.private_peer_user_id,
            sender_user_id=sender_user_id,
            sender_nickname=sender_nickname[:128],
            sender_group_card=sender_group_card[:128],
            direction=direction,
            event_kind=event_kind,
            source_plugin_id=source_plugin_id,
            external_source=external_source,
            external_event_key=external_event_key,
            external_event_type=external_event_type,
            external_payload_json=prepared_external_json,
            external_target_id=external_target_id,
            content=content,
            visual_summary="",
            segments_json=prepared_segments_json,
            reply_to_message_id=reply_to_message_id,
            reply_to_event_id=reply_to_event_id,
            origin=origin[:32],
            automation_id=automation_id,
            automation_run_id=automation_run_id,
            caused_by_event_id=caused_by_event_id,
            occurred_at=timestamp or observed_at,
            observed_at=observed_at,
        )
        trip("before_event_shadow")
        row.author_kind = AuthorKind.SYSTEM.value if plugin_external else AuthorKind.YUKI.value
        row.author_person_id = None
        row.author_account_id = None
        row.author_presence_id = None if plugin_external else presence.id
        row.canonical_event_id = canonical_event_id
        row.canonical_conversation_id = hydrated.conversation_id
        row.suppression_status = "keeper"
        row.ingress_presence_id = presence_id
        try:
            if plugin_external:
                async with session.begin_nested():
                    session.add(row)
                    await session.flush()
            else:
                session.add(row)
                await session.flush()
        except IntegrityError as exc:
            if not plugin_external:
                raise
            raced = await self._find_existing_plugin_external(
                session,
                scope=scope,
                source_plugin_id=source_plugin_id,
                external_event_key=external_event_key,
                external_target_id=external_target_id,
            )
            if raced is None:
                raise CanonicalIdentityError("receipt_conflict") from exc
            return await self._reuse_identical_live(
                session,
                raced,
                scope=scope,
                conversation=conversation,
                presence_id=presence_id,
                platform_message_id=platform_message_id,
                sender_user_id=sender_user_id,
                direction=direction,
                event_kind=event_kind,
                content=content,
                segments=segments,
                timestamp=timestamp,
                receipt=None,
                external_event_type=external_event_type,
                source_plugin_id=source_plugin_id,
                external_event_key=external_event_key,
                external_target_id=external_target_id,
                external_payload=external_payload,
                caused_by_event_id=caused_by_event_id,
            )
        await replace_event(
            session,
            row.id,
            hydrated.conversation_id,
            prepared_terms,
            author_person_id=row.author_person_id,
            author_kind=row.author_kind,
        )
        await touch_canonical_watermarks(
            session,
            hydrated.conversation_id,
            event_id=row.id,
        )
        conversation = await session.get(type(conversation), hydrated.conversation_id)
        if conversation is None:
            raise CanonicalIdentityError("unclassified")
        return ScopedAppendResult(
            event=_event_record(row),
            scope=await hydrate_scope_state_from_canonical(session, scope, conversation),
            created=True,
        )
