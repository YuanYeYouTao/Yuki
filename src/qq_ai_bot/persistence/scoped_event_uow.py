"""The only runtime write path for the permanent conversation event ledger."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from qq_ai_bot.conversation.canonical_db_models import (
    CanonicalConversationModel,
    CanonicalEventReceiptModel,
)
from qq_ai_bot.conversation.hydrate import (
    HydratedConversation,
    ensure_canonical_conversation,
    hydrate_scope_state_from_canonical,
    require_primary_alias_for_conversation,
    touch_canonical_watermarks,
)
from qq_ai_bot.conversation.initiative_sources import source_revision
from qq_ai_bot.conversation.media_index import prepare_event_media
from qq_ai_bot.conversation.scope_models import ConversationScopeState
from qq_ai_bot.domain.conversations import ConversationScope, ScopeType
from qq_ai_bot.domain.messages import InboundMessage
from qq_ai_bot.identity.canonical_repository import (
    apply_event_identity,
    external_id,
    find_identity_binding,
    find_space_binding,
)
from qq_ai_bot.identity.db_models import (
    CanonicalPersonModel,
    CanonicalSpaceModel,
    IdentityBindingModel,
    PresenceModel,
)
from qq_ai_bot.identity.errors import CanonicalIdentityError
from qq_ai_bot.identity.receipt_compat import (
    load_claimed_keeper,
    normalize_live_json,
    require_claimed_event,
    require_compatible_v2_live,
    require_reply_source,
)
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.models import ChatEventModel, ConversationMediaItemModel
from qq_ai_bot.persistence.repository_helpers import _event_record
from qq_ai_bot.persistence.repository_records import EventRecord


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
    ) -> ScopedAppendResult:
        """Persist one plugin/automation event through the scoped ledger boundary."""

        return await self.append(
            scope=scope,
            platform_message_id=platform_message_id,
            sender_user_id=scope.bot_user_id,
            direction="external",
            content=content,
            occurred_at=occurred_at,
            sender_is_bot=True,
            origin="plugin_background",
            event_kind="external_event",
            source_plugin_id=source_plugin_id,
            external_source=external_source,
            external_event_key=external_event_key,
            external_event_type=external_event_type,
            external_payload=external_payload,
            external_target_id=external_target_id,
            session=session,
        )

    async def append(
        self,
        *,
        scope: ConversationScope,
        platform_message_id: str,
        sender_user_id: str,
        direction: str,
        content: str,
        segments: tuple[dict[str, Any], ...] = (),
        reply_to_message_id: str | None = None,
        reply_to_event_id: int | None = None,
        occurred_at: datetime | None = None,
        sender_nickname: str = "",
        sender_group_card: str = "",
        sender_is_bot: bool = False,
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
        source_event_id: int | None = None,
    ) -> ScopedAppendResult:
        timestamp = occurred_at or datetime.now(UTC)
        observed_at = datetime.now(UTC)
        if session is not None:
            return await self._append_on_session(
                session,
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
                sender_is_bot=sender_is_bot,
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
                source_event_id=source_event_id,
            )
        async with self._database.immediate_session() as session:
            result = await self._append_canonical(
                session,
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
                sender_is_bot=sender_is_bot,
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
                source_event_id=source_event_id,
            )
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
            async with self._database.sessions() as reader:
                original = await reader.get(ChatEventModel, event_id)
                if (
                    original is None
                    or original.ingress_presence_id != presence_id
                    or original.platform_message_id != message.message_id
                    or original.sender_user_id != message.sender.user_id
                    or original.direction != "inbound"
                ):
                    raise CanonicalIdentityError("receipt_conflict")
                source_segments, source_content = original.segments_json, original.content
                conversation_id = original.canonical_conversation_id
                conversation = await reader.get(CanonicalConversationModel, conversation_id)
                presence = await reader.get(PresenceModel, presence_id)
                if (
                    conversation is None
                    or presence is None
                    or presence.platform != message.platform
                ):
                    raise CanonicalIdentityError("no_conversation")
                generation = conversation.generation
                mentions_query = (
                    select(
                        IdentityBindingModel.external_account_id,
                        IdentityBindingModel.person_id,
                        IdentityBindingModel.revision,
                    )
                    .where(
                        IdentityBindingModel.platform == presence.platform,
                        IdentityBindingModel.external_account_id.in_(message.mentioned_user_ids),
                        IdentityBindingModel.status == "active",
                    )
                    .order_by(IdentityBindingModel.external_account_id)
                )
                mention_facts = tuple((await reader.execute(mentions_query)).all())
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
            mentions = {account: person for account, person, _revision in mention_facts}
            context.update(
                mentioned_user_ids=list(message.mentioned_user_ids),
                mentioned_person_ids=list(
                    dict.fromkeys(
                        mentions[account]
                        for account in message.mentioned_user_ids
                        if account in mentions
                    )
                ),
                reply_sender_user_id=message.reply_sender_user_id,
                platform_edit_date=edited_at.timestamp(),
            )
            context["platform_update_id"] = revision
            if not metadata:
                metadata.append({"type": "yuki_context", "data": context})
            segments = json.dumps(
                [*message.segments, *metadata], ensure_ascii=False, separators=(",", ":")
            )
            media = prepare_event_media(
                event_id=event_id,
                conversation_id=conversation_id,
                generation=generation,
                segments=message.segments,
            )
            async with self._database.immediate_session() as session:
                event = await session.get(ChatEventModel, event_id)
                if event is None:
                    raise CanonicalIdentityError("receipt_conflict")
                if event.segments_json != source_segments or event.content != source_content:
                    continue
                conversation = await session.get(CanonicalConversationModel, conversation_id)
                if conversation is None:
                    raise CanonicalIdentityError("no_conversation")
                if (
                    conversation.generation != generation
                    or tuple((await session.execute(mentions_query)).all()) != mention_facts
                ):
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
                    if decoded[current.segment_index] != message.segments[prepared.segment_index]:
                        current.cache_status = "uncached"
                        current.content_sha256 = current.cache_name = None
                        current.cached_at = current.expires_at = None
                        current.created_at = prepared.created_at
                    current.segment_index = prepared.segment_index
                    current.kind = prepared.kind
                    current.display_name = prepared.display_name
                    current.declared_size = prepared.declared_size
                for removed in current_media.values():
                    await session.delete(removed)
                event.content, event.segments_json = message.text, segments
                event.visual_summary, event.audio_transcript = "", ""
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
        from qq_ai_bot.domain.audio import parse_transcripts

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
        async with self._database.sessions() as reader:
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
        receipt = await session.scalar(
            select(CanonicalEventReceiptModel).where(
                CanonicalEventReceiptModel.ingress_presence_id == presence_id,
                CanonicalEventReceiptModel.event_type == event_type,
                CanonicalEventReceiptModel.platform_message_id == platform_message_id[:128],
            )
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
        presence_id: str,
        event_type: str,
        platform_message_id: str,
    ) -> tuple[CanonicalEventReceiptModel | None, ChatEventModel | None]:
        receipt = await session.scalar(
            select(CanonicalEventReceiptModel).where(
                CanonicalEventReceiptModel.ingress_presence_id == presence_id,
                CanonicalEventReceiptModel.event_type == event_type,
                CanonicalEventReceiptModel.platform_message_id == platform_message_id[:128],
            )
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
        direction: str,
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

    async def _expected_author(
        self,
        session: AsyncSession,
        *,
        scope: ConversationScope,
        sender_user_id: str,
        sender_is_bot: bool,
        event_kind: str,
        direction: str,
        presence_id: str,
    ) -> tuple[str, str | None, str | None]:
        from qq_ai_bot.domain.identity import AuthorKind
        from qq_ai_bot.identity.event_author import project_event_author

        del scope
        presence = await session.get(PresenceModel, presence_id)
        if presence is None:
            raise CanonicalIdentityError("no_presence")
        author = await project_event_author(
            session,
            sender_user_id=sender_user_id,
            sender_is_bot=sender_is_bot,
            event_kind=event_kind,
            direction=direction,
            platform=presence.platform,
        )
        if author.author_kind == AuthorKind.PERSON.value and author.author_person_id is None:
            raise CanonicalIdentityError("receipt_conflict")
        return author.as_tuple()

    def _require_compatible_live(
        self,
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
        sender_is_bot: bool,
        direction: str,
        event_kind: str,
        content: str,
        segments: tuple[dict[str, Any], ...],
        timestamp: datetime,
        receipt: CanonicalEventReceiptModel | None,
        external_event_type: str | None,
        source_plugin_id: str | None = None,
        external_event_key: str | None = None,
        external_target_id: str | None = None,
        external_payload: dict[str, Any] | None = None,
        caused_by_event_id: int | None = None,
    ) -> ScopedAppendResult:
        author_kind, author_person_id, author_presence_id = await self._expected_author(
            session,
            scope=scope,
            sender_user_id=sender_user_id,
            sender_is_bot=sender_is_bot,
            event_kind=event_kind,
            direction=direction,
            presence_id=presence_id,
        )
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
            timestamp=timestamp,
            author_kind=author_kind,
            author_person_id=author_person_id,
            author_presence_id=author_presence_id,
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

    async def _append_on_session(
        self,
        session: AsyncSession,
        *,
        scope: ConversationScope,
        platform_message_id: str,
        sender_user_id: str,
        direction: str,
        content: str,
        segments: tuple[dict[str, Any], ...],
        reply_to_message_id: str | None,
        reply_to_event_id: int | None,
        timestamp: datetime,
        observed_at: datetime,
        sender_nickname: str,
        sender_group_card: str,
        sender_is_bot: bool,
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
        source_event_id: int | None = None,
    ) -> ScopedAppendResult:
        return await self._append_canonical(
            session,
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
            sender_is_bot=sender_is_bot,
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
            source_event_id=source_event_id,
        )

    async def _append_canonical(
        self,
        session: AsyncSession,
        *,
        scope: ConversationScope,
        platform_message_id: str,
        sender_user_id: str,
        direction: str,
        content: str,
        segments: tuple[dict[str, Any], ...],
        reply_to_message_id: str | None,
        reply_to_event_id: int | None,
        timestamp: datetime,
        observed_at: datetime,
        sender_nickname: str,
        sender_group_card: str,
        sender_is_bot: bool,
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
        source_event_id: int | None = None,
    ) -> ScopedAppendResult:
        from uuid import uuid4

        from sqlalchemy.exc import IntegrityError

        presence = (
            await session.get(PresenceModel, scope.presence_id)
            if scope.presence_id
            else (
                await session.execute(
                    select(PresenceModel).where(
                        PresenceModel.external_account_id == external_id(scope.bot_user_id),
                        PresenceModel.platform == scope.platform,
                    )
                )
            ).scalar_one_or_none()
        )
        if presence is None:
            raise CanonicalIdentityError("no_presence")
        presence_id = presence.id
        if scope.conversation_id is not None:
            original = await session.get(CanonicalConversationModel, scope.conversation_id)
            if original is None:
                raise CanonicalIdentityError("no_conversation")
            if original.kind == "private":
                binding = await session.get(IdentityBindingModel, original.identity_binding_id)
                from qq_ai_bot.conversation.legacy_entries import legacy_private_entry_matches

                legacy_entry = (
                    binding is not None
                    and direction == "outbound"
                    and await legacy_private_entry_matches(
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
                        and not legacy_entry
                    )
                ):
                    raise CanonicalIdentityError("canonical_owner_mismatch")
            else:
                from qq_ai_bot.identity.db_models import SpaceBindingModel

                if scope.scope_type is not ScopeType.GROUP or scope.group_id is None:
                    raise CanonicalIdentityError("canonical_owner_mismatch")
                entry_space_binding = (
                    await session.get(SpaceBindingModel, original.space_binding_id)
                    if original.space_binding_id
                    else await find_space_binding(
                        session, scope.group_id, platform=presence.platform
                    )
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
                space_binding = await find_space_binding(
                    session, external_id(scope.group_id), platform=presence.platform
                )
                space = (
                    None
                    if space_binding is None or space_binding.status != "active"
                    else await session.get(CanonicalSpaceModel, space_binding.space_id)
                )
                if space is None or not space.enabled or space_binding is None:
                    raise CanonicalIdentityError("no_space_binding")
                hydrated = await ensure_canonical_conversation(
                    session,
                    kind="space",
                    primary_scope_key=scope.key,
                    space_id=space.id,
                    space_binding_id=space_binding.id,
                    thread_id=scope.thread_id,
                )
            else:
                person_binding = (
                    await session.get(IdentityBindingModel, identity_binding_id)
                    if identity_binding_id is not None
                    else await find_identity_binding(
                        session,
                        external_id(scope.private_peer_user_id or sender_user_id),
                        platform=presence.platform,
                    )
                )
                if identity_binding_id is not None and (
                    direction != "outbound"
                    or person_binding is None
                    or person_binding.platform != presence.platform
                    or person_binding.external_account_id != scope.private_peer_user_id
                ):
                    raise CanonicalIdentityError("canonical_owner_mismatch")
                person = (
                    None
                    if person_binding is None
                    or (identity_binding_id is None and person_binding.status != "active")
                    else await session.get(CanonicalPersonModel, person_binding.person_id)
                )
                if person is None or (identity_binding_id is None and not person.enabled):
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
                sender_is_bot=sender_is_bot,
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
        canonical_event_id = str(uuid4())
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
                    sender_is_bot=sender_is_bot,
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
            external_payload_json=(
                json.dumps(external_payload, ensure_ascii=False, separators=(",", ":"))
                if external_payload is not None
                else None
            ),
            external_target_id=external_target_id,
            content=content,
            visual_summary="",
            segments_json=json.dumps(segments, ensure_ascii=False, separators=(",", ":")),
            reply_to_message_id=reply_to_message_id,
            reply_to_event_id=reply_to_event_id,
            origin=origin[:32],
            automation_id=automation_id,
            automation_run_id=automation_run_id,
            caused_by_event_id=caused_by_event_id,
            occurred_at=timestamp,
            observed_at=observed_at,
        )
        await apply_event_identity(session, row, sender_is_bot=sender_is_bot, ingress=presence)
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
                sender_is_bot=sender_is_bot,
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
        await touch_canonical_watermarks(
            session,
            hydrated.conversation_id,
            event_id=row.id,
        )
        conversation = await session.get(type(conversation), hydrated.conversation_id)
        if conversation is None:
            raise CanonicalIdentityError("unclassified")
        if direction == "inbound" and event_kind == "message":
            from qq_ai_bot.conversation.media_index import index_event_media

            await index_event_media(
                session,
                event_id=row.id,
                conversation_id=hydrated.conversation_id,
                generation=int(conversation.generation),
                segments=segments,
            )
        return ScopedAppendResult(
            event=_event_record(row),
            scope=await hydrate_scope_state_from_canonical(session, scope, conversation),
            created=True,
        )
