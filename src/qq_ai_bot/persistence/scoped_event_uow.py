"""The only runtime write path for the permanent conversation event ledger."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from qq_ai_bot.conversation.canonical_db_models import (
    CanonicalConversationModel,
    CanonicalConversationRollupModel,
    CanonicalEventReceiptModel,
)
from qq_ai_bot.conversation.hydrate import (
    bump_canonical_generation,
    delete_legacy_rollup_projections,
    ensure_canonical_conversation,
    hydrate_scope_state_from_canonical,
    touch_canonical_watermarks,
)
from qq_ai_bot.conversation.rollup.db_models import (
    ConversationRollupJobModel,
    ConversationRollupModel,
)
from qq_ai_bot.conversation.rollup.metrics import ConversationRollupMetrics
from qq_ai_bot.conversation.rollup.models import ConversationScopeState, RollupPolicyConfig
from qq_ai_bot.conversation.rollup.prompt_accounting import (
    prompt_accounting_event_characters,
)
from qq_ai_bot.conversation.rollup.repository import (
    _scope_from_row,
    _scope_state,
    eligible_prefix,
    exceeds_high_watermark,
    get_or_create_scope_row,
    recount_canonical_uncovered,
    recount_scope_uncovered,
)
from qq_ai_bot.domain.conversations import ConversationScope, ScopeType
from qq_ai_bot.domain.messages import InboundMessage
from qq_ai_bot.identity.dual_write import AccountRole, apply_event_identity_shadows
from qq_ai_bot.identity.errors import IdentityDualWriteError
from qq_ai_bot.identity.receipt_compat import (
    load_claimed_keeper,
    normalize_live_json,
    require_claimed_event,
    require_compatible_v2_live,
)
from qq_ai_bot.identity.write_settings import identity_write_settings
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.models import ChatEventModel
from qq_ai_bot.persistence.repository_helpers import _ensure_group, _ensure_person, _event_record
from qq_ai_bot.persistence.repository_records import EventRecord


@dataclass(frozen=True, slots=True)
class ScopedAppendResult:
    event: EventRecord
    scope: ConversationScopeState
    created: bool
    job_signalled: bool


@dataclass(frozen=True, slots=True)
class NewGenerationResult:
    event: EventRecord
    scope: ConversationScopeState
    generation_changed: bool


def _legacy_v1_sender_account_role(
    *,
    sender_user_id: str,
    bot_user_id: str,
    sender_is_bot: bool,
    ignored_bot_users: frozenset[str],
) -> AccountRole:
    """v1 people-row role: current handle is Yuki. complete-v2 never reaches here."""

    if sender_user_id == bot_user_id:
        return "yuki_self"
    if sender_is_bot or sender_user_id in ignored_bot_users:
        return "external_bot"
    return "human"


def _legacy_v1_private_peer_account_role(
    *,
    private_peer_user_id: str,
    bot_user_id: str,
    ignored_bot_users: frozenset[str],
) -> AccountRole:
    """v1 private-peer role. complete-v2 never reaches here."""

    if private_peer_user_id == bot_user_id:
        return "yuki_self"
    if private_peer_user_id in ignored_bot_users:
        return "external_bot"
    return "private_peer"


class ScopedEventLedgerUnitOfWork:
    """Commit event, scope counters, and the single job signal atomically."""

    def __init__(
        self,
        database: Database,
        *,
        config: RollupPolicyConfig,
        notify_worker: Callable[[], None] | None = None,
        metrics: ConversationRollupMetrics | None = None,
    ) -> None:
        self._database = database
        self._config = config
        self._notify_worker = notify_worker
        self.metrics = metrics or ConversationRollupMetrics()

    def set_worker_notifier(self, notify_worker: Callable[[], None] | None) -> None:
        self._notify_worker = notify_worker

    async def append_inbound(self, message: InboundMessage) -> ScopedAppendResult:
        scope = message.scope()
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
        return await self.append(
            scope=scope,
            platform_message_id=message.message_id,
            sender_user_id=message.sender.user_id,
            direction="inbound",
            content=message.text,
            segments=tuple(segments),
            reply_to_message_id=message.reply_to_message_id,
            occurred_at=message.received_at,
            sender_nickname=message.sender.nickname,
            sender_group_card=message.sender.group_card,
            sender_is_bot=message.sender.is_bot,
        )

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
        session: AsyncSession | None = None,
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
            )
        async with self._database.immediate_session() as session:
            from qq_ai_bot.identity.runtime import identity_runtime_is_complete_v2

            if await identity_runtime_is_complete_v2(session):
                return await self._append_complete_v2(
                    session,
                    scope=scope,
                    platform_message_id=platform_message_id,
                    sender_user_id=sender_user_id,
                    direction=direction,
                    content=content,
                    segments=segments,
                    reply_to_message_id=reply_to_message_id,
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
                )
            result = await self._append_legacy_v1_on_session(
                session,
                scope=scope,
                platform_message_id=platform_message_id,
                sender_user_id=sender_user_id,
                direction=direction,
                content=content,
                segments=segments,
                reply_to_message_id=reply_to_message_id,
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
            )
        self._notify_after_commit(result.job_signalled)
        return result

    async def append_new_generation_command(
        self,
        *,
        scope: ConversationScope,
        inbound: InboundMessage,
    ) -> NewGenerationResult:
        """Append `/ai new` and switch generation in the same short transaction."""

        now = datetime.now(UTC)
        async with self._database.immediate_session() as session:
            from qq_ai_bot.identity.runtime import identity_runtime_is_complete_v2

            if await identity_runtime_is_complete_v2(session):
                appended = await self._append_complete_v2(
                    session,
                    scope=scope,
                    platform_message_id=inbound.message_id,
                    sender_user_id=inbound.sender.user_id,
                    direction="inbound",
                    content=inbound.text,
                    segments=tuple(inbound.segments),
                    reply_to_message_id=inbound.reply_to_message_id,
                    timestamp=inbound.received_at,
                    observed_at=now,
                    sender_nickname=inbound.sender.nickname,
                    sender_group_card=inbound.sender.group_card,
                    sender_is_bot=inbound.sender.is_bot,
                    origin="user_message",
                    automation_id=None,
                    automation_run_id=None,
                    event_kind="message",
                    source_plugin_id=None,
                    external_source=None,
                    external_event_key=None,
                    external_event_type=None,
                    external_payload=None,
                    external_target_id=None,
                )
                event_row = await session.get(ChatEventModel, appended.event.id)
                conversation_id = None if event_row is None else event_row.canonical_conversation_id
                if conversation_id:
                    conversation = await session.get(CanonicalConversationModel, conversation_id)
                    if conversation is not None:
                        prior_change = int(conversation.last_generation_change_event_id)
                        await bump_canonical_generation(
                            session,
                            conversation.id,
                            event_id=appended.event.id,
                        )
                        conversation = await session.get(
                            CanonicalConversationModel, conversation.id
                        )
                        assert conversation is not None
                        return NewGenerationResult(
                            event=appended.event,
                            scope=await hydrate_scope_state_from_canonical(
                                session, scope, conversation
                            ),
                            generation_changed=prior_change != appended.event.id,
                        )
                return NewGenerationResult(
                    event=appended.event,
                    scope=appended.scope,
                    generation_changed=False,
                )
            await self._ensure_identities(
                session,
                scope=scope,
                sender_user_id=inbound.sender.user_id,
                sender_nickname=inbound.sender.nickname,
                sender_is_bot=inbound.sender.is_bot,
                timestamp=inbound.received_at,
                observed_at=now,
            )
            row = await session.scalar(
                select(ChatEventModel).where(
                    ChatEventModel.bot_user_id == scope.bot_user_id,
                    ChatEventModel.platform_message_id == inbound.message_id,
                )
            )
            if row is None:
                row = ChatEventModel(
                    bot_user_id=scope.bot_user_id,
                    platform_message_id=inbound.message_id,
                    scope_type=scope.scope_type.value,
                    group_id=scope.group_id,
                    private_peer_user_id=scope.private_peer_user_id,
                    sender_user_id=inbound.sender.user_id,
                    sender_nickname=inbound.sender.nickname[:128],
                    sender_group_card=inbound.sender.group_card[:128],
                    direction="inbound",
                    event_kind="message",
                    content=inbound.text,
                    visual_summary="",
                    segments_json=json.dumps(
                        inbound.segments, ensure_ascii=False, separators=(",", ":")
                    ),
                    reply_to_message_id=inbound.reply_to_message_id,
                    origin="user_message",
                    occurred_at=inbound.received_at,
                    observed_at=now,
                )
                await apply_event_identity_shadows(
                    session, row, sender_is_bot=inbound.sender.is_bot
                )
                session.add(row)
                await session.flush()
            scope_row = await get_or_create_scope_row(
                session, scope, first_event_id=row.id, now=now
            )
            changed = scope_row.last_generation_change_event_id != row.id
            if changed:
                scope_row.generation += 1
                scope_row.starts_after_event_id = row.id
                scope_row.last_generation_change_event_id = row.id
                scope_row.last_event_id = max(scope_row.last_event_id, row.id)
                scope_row.uncovered_event_count = 0
                scope_row.uncovered_character_count = 0
                scope_row.updated_at = now
                await delete_legacy_rollup_projections(session, scope_row.id)
            state = _scope_state(scope_row)
            event = _event_record(row)
        return NewGenerationResult(event=event, scope=state, generation_changed=changed)

    async def set_visual_summary(self, event_id: int, summary: str) -> bool:
        normalized = summary.strip()[:6000]
        lowered = normalized.casefold()
        if "data:image/" in lowered or "base64://" in lowered:
            raise ValueError("visual_summary must not contain image or Base64 payloads")
        now = datetime.now(UTC)
        signalled = False
        async with self._database.immediate_session() as session:
            row = await session.get(ChatEventModel, event_id)
            if row is None:
                return False
            from qq_ai_bot.identity.runtime import identity_runtime_is_complete_v2

            if await identity_runtime_is_complete_v2(session):
                old = _event_record(row)
                old_characters = prompt_accounting_event_characters(
                    old,
                    events=(old,),
                    bot_display_name=self._config.bot_display_name,
                    timezone=self._config.timezone,
                )
                row.visual_summary = normalized
                await session.flush()
                new = _event_record(row)
                conversation_id = row.canonical_conversation_id
                if conversation_id:
                    conversation = await session.get(CanonicalConversationModel, conversation_id)
                    if conversation is not None:
                        canonical_rollup = await session.get(
                            CanonicalConversationRollupModel, conversation.id
                        )
                        coverage = (
                            canonical_rollup.covered_through_event_id
                            if canonical_rollup is not None
                            and canonical_rollup.generation == conversation.generation
                            else conversation.starts_after_event_id
                        )
                        if row.id > coverage:
                            next_characters = conversation.uncovered_character_count + (
                                prompt_accounting_event_characters(
                                    new,
                                    events=(new,),
                                    bot_display_name=self._config.bot_display_name,
                                    timezone=self._config.timezone,
                                )
                                - old_characters
                            )
                            if next_characters < 0:
                                await recount_canonical_uncovered(
                                    session, conversation, self._config
                                )
                                self.metrics.counter_repairs += 1
                                if conversation.uncovered_character_count < 0:
                                    self.metrics.counter_reconcile_failures += 1
                                    raise RuntimeError("visual projection counter recount failed")
                            else:
                                conversation.uncovered_character_count = next_characters
                            conversation.updated_at = now
                            from qq_ai_bot.conversation.canonical_rollup import (
                                signal_canonical_rollup_if_needed,
                            )

                            signalled = await signal_canonical_rollup_if_needed(
                                session, conversation, self._config, force_existing=True
                            )
                        else:
                            self.metrics.late_visual_after_coverage += 1
            else:
                old = _event_record(row)
                scope = self._scope_for_event(old)
                scope_row = await get_or_create_scope_row(
                    session, scope, first_event_id=row.id, now=now
                )
                old_characters = prompt_accounting_event_characters(
                    old,
                    events=(old,),
                    bot_display_name=self._config.bot_display_name,
                    timezone=self._config.timezone,
                )
                row.visual_summary = normalized
                await session.flush()
                new = _event_record(row)
                legacy_rollup = await session.get(ConversationRollupModel, scope_row.id)
                coverage = (
                    legacy_rollup.covered_through_event_id
                    if legacy_rollup is not None
                    and legacy_rollup.generation == scope_row.generation
                    else scope_row.starts_after_event_id
                )
                if row.id > coverage:
                    scope_row.uncovered_character_count += (
                        prompt_accounting_event_characters(
                            new,
                            events=(new,),
                            bot_display_name=self._config.bot_display_name,
                            timezone=self._config.timezone,
                        )
                        - old_characters
                    )
                    if scope_row.uncovered_character_count < 0:
                        await recount_scope_uncovered(session, scope_row, self._config)
                        self.metrics.counter_repairs += 1
                        if scope_row.uncovered_character_count < 0:
                            self.metrics.counter_reconcile_failures += 1
                            raise RuntimeError("visual projection counter recount failed")
                    scope_row.updated_at = now
                    signalled = await self._signal_if_needed(
                        session, scope_row, force_existing=True
                    )
                else:
                    self.metrics.late_visual_after_coverage += 1
        self._notify_after_commit(signalled)
        return True

    async def _signal_if_needed(
        self,
        session: AsyncSession,
        scope_row: Any,
        *,
        force_existing: bool,
    ) -> bool:
        job = await session.get(ConversationRollupJobModel, scope_row.id)
        now = datetime.now(UTC)
        if job is not None:
            if force_existing:
                job.signal_revision += 1
                job.next_attempt_at = now
                job.updated_at = now
                return True
            return False
        scope = _scope_from_row(scope_row)
        rollup = await session.get(ConversationRollupModel, scope_row.id)
        coverage = (
            rollup.covered_through_event_id
            if rollup is not None and rollup.generation == scope_row.generation
            else scope_row.starts_after_event_id
        )
        rows = tuple(
            (
                await session.scalars(
                    select(ChatEventModel)
                    .where(
                        ChatEventModel.bot_user_id == scope.bot_user_id,
                        ChatEventModel.scope_type == scope.scope_type.value,
                        ChatEventModel.group_id == scope.group_id,
                        ChatEventModel.private_peer_user_id == scope.private_peer_user_id,
                        ChatEventModel.id > coverage,
                        ChatEventModel.id <= scope_row.last_event_id,
                    )
                    .order_by(ChatEventModel.id.asc())
                )
            ).all()
        )
        events = tuple(_event_record(row) for row in rows)
        if not exceeds_high_watermark(eligible_prefix(events, self._config), self._config):
            return False
        session.add(
            ConversationRollupJobModel(
                scope_id=scope_row.id,
                generation=scope_row.generation,
                signal_revision=1,
                status="pending",
                failure_count=0,
                lease_owner=None,
                lease_token=None,
                lease_until=None,
                next_attempt_at=now,
                last_error_category=None,
                created_at=now,
                updated_at=now,
            )
        )
        return True

    async def _find_existing_v2_live(
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
            return await self._find_existing_v2_plugin_external(
                session,
                scope=scope,
                source_plugin_id=source_plugin_id,
                external_event_key=external_event_key,
                external_target_id=external_target_id,
            ), None
        existing = await session.scalar(
            select(ChatEventModel).where(
                ChatEventModel.bot_user_id == scope.bot_user_id,
                ChatEventModel.platform_message_id == platform_message_id,
                ChatEventModel.canonical_event_id.is_(None),
            )
        )
        return existing, None

    async def _find_existing_v2_plugin_external(
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
            raise IdentityDualWriteError("receipt_conflict")
        rows = list(
            (
                await session.scalars(
                    select(ChatEventModel).where(
                        ChatEventModel.event_kind == "external_event",
                        ChatEventModel.source_plugin_id == source_plugin_id,
                        ChatEventModel.external_event_key == external_event_key,
                        ChatEventModel.scope_type == scope.scope_type.value,
                        ChatEventModel.external_target_id == external_target_id,
                    )
                )
            ).all()
        )
        if len(rows) > 1:
            raise IdentityDualWriteError("receipt_conflict")
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

    async def _expected_v2_author(
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
        from qq_ai_bot.identity.event_author import project_complete_v2_event_author

        del scope, presence_id
        author = await project_complete_v2_event_author(
            session,
            sender_user_id=sender_user_id,
            sender_is_bot=sender_is_bot,
            event_kind=event_kind,
            direction=direction,
        )
        if author.author_kind == AuthorKind.PERSON.value and author.author_person_id is None:
            raise IdentityDualWriteError("receipt_conflict")
        return author.as_tuple()

    def _require_compatible_v2_live(
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
            raise IdentityDualWriteError("receipt_conflict")

    async def _reuse_identical_v2_live(
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
    ) -> ScopedAppendResult:
        author_kind, author_person_id, author_presence_id = await self._expected_v2_author(
            session,
            scope=scope,
            sender_user_id=sender_user_id,
            sender_is_bot=sender_is_bot,
            event_kind=event_kind,
            direction=direction,
            presence_id=presence_id,
        )
        self._require_compatible_v2_live(
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
        )
        return ScopedAppendResult(
            event=_event_record(existing),
            scope=await hydrate_scope_state_from_canonical(session, scope, conversation),
            created=False,
            job_signalled=False,
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
    ) -> ScopedAppendResult:
        from qq_ai_bot.identity.runtime import identity_runtime_is_complete_v2

        if await identity_runtime_is_complete_v2(session):
            return await self._append_complete_v2(
                session,
                scope=scope,
                platform_message_id=platform_message_id,
                sender_user_id=sender_user_id,
                direction=direction,
                content=content,
                segments=segments,
                reply_to_message_id=reply_to_message_id,
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
            )
        return await self._append_legacy_v1_on_session(
            session,
            scope=scope,
            platform_message_id=platform_message_id,
            sender_user_id=sender_user_id,
            direction=direction,
            content=content,
            segments=segments,
            reply_to_message_id=reply_to_message_id,
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
        )

    async def _append_legacy_v1_on_session(
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
    ) -> ScopedAppendResult:
        existing = await session.scalar(
            select(ChatEventModel).where(
                ChatEventModel.bot_user_id == scope.bot_user_id,
                ChatEventModel.platform_message_id == platform_message_id,
            )
        )
        if existing is not None:
            if self._scope_for_event(_event_record(existing)) != scope:
                raise RuntimeError("platform message id already belongs to another scope")
            scope_row = await get_or_create_scope_row(
                session, scope, first_event_id=existing.id, now=observed_at
            )
            return ScopedAppendResult(
                event=_event_record(existing),
                scope=_scope_state(scope_row),
                created=False,
                job_signalled=False,
            )
        await self._ensure_identities(
            session,
            scope=scope,
            sender_user_id=sender_user_id,
            sender_nickname=sender_nickname,
            sender_is_bot=sender_is_bot,
            timestamp=timestamp,
            observed_at=observed_at,
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
            origin=origin[:32],
            automation_id=automation_id,
            automation_run_id=automation_run_id,
            occurred_at=timestamp,
            observed_at=observed_at,
        )
        await apply_event_identity_shadows(session, row, sender_is_bot=sender_is_bot)
        session.add(row)
        await session.flush()
        event = _event_record(row)
        scope_row = await get_or_create_scope_row(
            session, scope, first_event_id=row.id, now=observed_at
        )
        scope_row.last_event_id = max(scope_row.last_event_id, row.id)
        scope_row.uncovered_event_count += 1
        scope_row.uncovered_character_count += prompt_accounting_event_characters(
            event,
            events=(event,),
            bot_display_name=self._config.bot_display_name,
            timezone=self._config.timezone,
        )
        scope_row.updated_at = observed_at
        signalled = await self._signal_if_needed(session, scope_row, force_existing=True)
        return ScopedAppendResult(
            event=_event_record(row),
            scope=_scope_state(scope_row),
            created=True,
            job_signalled=signalled,
        )

    async def _append_complete_v2(
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
    ) -> ScopedAppendResult:
        from uuid import uuid4

        from sqlalchemy.exc import IntegrityError

        from qq_ai_bot.identity.shadows import (
            active_person_id_for,
            active_space_id_for,
            presence_id_for,
        )

        presence_id = await presence_id_for(session, scope.bot_user_id)
        if presence_id is None:
            raise IdentityDualWriteError("no_presence")
        if scope.scope_type is ScopeType.GROUP:
            space_id = await active_space_id_for(session, scope.group_id)
            if space_id is None:
                raise IdentityDualWriteError("no_space_binding")
            hydrated = await ensure_canonical_conversation(
                session,
                kind="space",
                primary_scope_key=scope.key,
                space_id=space_id,
            )
        else:
            person_id = await active_person_id_for(
                session, scope.private_peer_user_id or sender_user_id
            )
            if person_id is None:
                raise IdentityDualWriteError("no_person")
            hydrated = await ensure_canonical_conversation(
                session,
                kind="private",
                primary_scope_key=scope.key,
                person_id=person_id,
            )
        receipt_event_type = (external_event_type or "message")[:64]
        existing, receipt = await self._find_existing_v2_live(
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
            raise IdentityDualWriteError("unclassified")
        if existing is not None:
            return await self._reuse_identical_v2_live(
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
                    raise IdentityDualWriteError("receipt_conflict") from exc
                return await self._reuse_identical_v2_live(
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
            origin=origin[:32],
            automation_id=automation_id,
            automation_run_id=automation_run_id,
            occurred_at=timestamp,
            observed_at=observed_at,
        )
        await apply_event_identity_shadows(session, row, sender_is_bot=sender_is_bot)
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
            raced = await self._find_existing_v2_plugin_external(
                session,
                scope=scope,
                source_plugin_id=source_plugin_id,
                external_event_key=external_event_key,
                external_target_id=external_target_id,
            )
            if raced is None:
                raise IdentityDualWriteError("receipt_conflict") from exc
            return await self._reuse_identical_v2_live(
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
            )
        event = _event_record(row)
        await touch_canonical_watermarks(
            session,
            hydrated.conversation_id,
            event_id=row.id,
            characters=prompt_accounting_event_characters(
                event,
                events=(event,),
                bot_display_name=self._config.bot_display_name,
                timezone=self._config.timezone,
            ),
        )
        conversation = await session.get(type(conversation), hydrated.conversation_id)
        if conversation is None:
            raise IdentityDualWriteError("unclassified")
        from qq_ai_bot.conversation.canonical_rollup import signal_canonical_rollup_if_needed

        signalled = await signal_canonical_rollup_if_needed(
            session, conversation, self._config, force_existing=True
        )
        return ScopedAppendResult(
            event=_event_record(row),
            scope=await hydrate_scope_state_from_canonical(session, scope, conversation),
            created=True,
            job_signalled=signalled,
        )

    @staticmethod
    async def _ensure_identities(
        session: AsyncSession,
        *,
        scope: ConversationScope,
        sender_user_id: str,
        sender_nickname: str,
        sender_is_bot: bool,
        timestamp: datetime,
        observed_at: datetime,
    ) -> None:
        from qq_ai_bot.identity.runtime import identity_runtime_is_complete_v2

        if await identity_runtime_is_complete_v2(session):
            return
        settings = identity_write_settings()
        sender_role = _legacy_v1_sender_account_role(
            sender_user_id=sender_user_id,
            bot_user_id=scope.bot_user_id,
            sender_is_bot=sender_is_bot,
            ignored_bot_users=settings.ignored_bot_users,
        )
        await _ensure_person(
            session,
            sender_user_id,
            nickname=sender_nickname,
            is_bot=sender_is_bot,
            now=timestamp,
            canonical_role=sender_role,
        )
        await _ensure_person(
            session,
            scope.bot_user_id,
            is_bot=True,
            now=observed_at,
            canonical_role="yuki_self",
        )
        if scope.private_peer_user_id:
            peer_role = _legacy_v1_private_peer_account_role(
                private_peer_user_id=scope.private_peer_user_id,
                bot_user_id=scope.bot_user_id,
                ignored_bot_users=settings.ignored_bot_users,
            )
            await _ensure_person(
                session,
                scope.private_peer_user_id,
                now=timestamp,
                canonical_role=peer_role,
            )
        if scope.group_id:
            await _ensure_group(session, scope.group_id, now=timestamp)

    @staticmethod
    def _scope_for_event(event: EventRecord) -> ConversationScope:
        if event.scope_type is ScopeType.GROUP:
            return ConversationScope.group(event.bot_user_id, event.group_id or "")
        return ConversationScope.private(
            event.bot_user_id, event.private_peer_user_id or event.sender_user_id
        )

    def _notify_after_commit(self, signalled: bool) -> None:
        if signalled and self._notify_worker is not None:
            try:
                self._notify_worker()
            except Exception:
                # Durable polling is authoritative; the process-local wakeup
                # is only a latency optimization.
                return
