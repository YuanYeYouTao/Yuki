"""Transactional persistence for plugin external events, delivery, and background turns."""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from qq_ai_bot.conversation.canonical_db_models import (
    CanonicalConversationModel,
    CanonicalConversationRollupEmergencyOverlayModel,
    CanonicalConversationRollupModel,
)
from qq_ai_bot.conversation.rollup.coverage import session_effective_coverage
from qq_ai_bot.conversation.rollup.models import RollupPolicyConfig
from qq_ai_bot.domain.conversations import ConversationScope
from qq_ai_bot.domain.identity import AuthorKind
from qq_ai_bot.identity.errors import CanonicalIdentityError
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.models import ChatEventModel
from qq_ai_bot.persistence.repository_helpers import (
    keeper_event_clause,
    suppression_is_canonical_live,
)
from qq_ai_bot.persistence.scoped_event_uow import ScopedEventLedgerUnitOfWork
from qq_ai_bot.plugin_host.db_models import (
    PluginBackgroundTargetGrantModel,
    PluginBackgroundTurnJobModel,
    PluginInstallationModel,
    PluginMediaArtifactModel,
    PluginNotificationOutboxModel,
)
from qq_ai_bot.plugin_host.ownership import (
    CANONICAL_OWNER_DISABLED,
    CANONICAL_OWNER_MISMATCH,
    MISSING_CANONICAL_OWNER,
    STATE_MISMATCH,
    PluginOwnershipError,
    find_grant_lineage,
    inherit_publication_canonicals,
    plugin_ownership_error,
    require_grant_readable,
    require_inherited_publication,
    require_live_conversation,
    require_live_person,
    require_live_space,
    require_presence_provenance,
    resolve_grant_target_owners,
    resolve_human_person_id,
    stamp_grant_owners,
)
from yuki_plugin_sdk.errors import PluginPermissionError
from yuki_plugin_sdk.models import (
    BackgroundTargetGrantView,
    NotificationPublishReceipt,
    NotificationTarget,
    PublishNotificationRequest,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class OutboxRecord:
    id: int
    notification_id: str
    source_event_id: int
    plugin_id: str
    target_type: str
    target_id: str
    bot_user_id: str
    part_type: str
    text: str
    media_handle_id: str | None
    attempts: int
    canonical_target_person_id: str | None
    canonical_target_space_id: str | None
    canonical_presence_id: str | None
    canonical_conversation_id: str | None


OUTBOX_LEASE_SECONDS = 300
TURN_LEASE_SECONDS = 900
TURN_ERROR_SUPERSEDED_COVERED = "superseded_covered"
TURN_ERROR_GENERATION_STALE = "generation_stale"
TURN_ERROR_LATER_HUMAN = "later_human_inbound"
TURN_ERROR_ATTEMPT_RECLAIMED = "attempt_reclaimed"


class BackgroundTurnFenceError(RuntimeError):
    """The claimed attempt may not call the provider or emit a reply."""

    def __init__(self, category: str) -> None:
        super().__init__(category)
        self.category = category


@dataclass(frozen=True, slots=True)
class BackgroundTurnJobRecord:
    id: int
    source_event_id: int
    plugin_id: str
    target_type: str
    target_id: str
    bot_user_id: str
    agent_intent: str
    attempts: int
    generation: int
    canonical_target_person_id: str | None
    canonical_target_space_id: str | None
    canonical_presence_id: str | None
    canonical_conversation_id: str | None


@dataclass(frozen=True, slots=True)
class QueuedCanonicalContext:
    person_id: str | None
    space_id: str | None
    conversation_id: str
    provenance_presence_id: str | None
    creator_person_id: str
    primary_alias: str
    generation: int
    scope_id: int


class PluginNotificationRepository:
    """Keep publication idempotency and delivery work in Host-owned transactions."""

    def __init__(
        self,
        database: Database,
        scoped_events: ScopedEventLedgerUnitOfWork | None = None,
    ) -> None:
        self._database = database
        self._scoped_events = scoped_events or ScopedEventLedgerUnitOfWork(
            database,
            config=RollupPolicyConfig(),
        )

    async def grant_target(
        self,
        *,
        plugin_id: str,
        target: NotificationTarget,
        bot_user_id: str,
        created_by_user_id: str,
    ) -> BackgroundTargetGrantView:
        now = datetime.now(UTC)
        async with self._database.sessions() as session, session.begin():
            person_id, space_id = await _grant_owners(
                session,
                created_by_user_id=created_by_user_id,
                target=target,
            )
            row = await find_grant_lineage(
                session,
                plugin_id=plugin_id,
                target_type=target.target_type,
                person_id=person_id,
                space_id=space_id,
            )
            if row is None:
                row = PluginBackgroundTargetGrantModel(
                    plugin_id=plugin_id,
                    target_type=target.target_type,
                    target_id=target.target_id,
                    bot_user_id=bot_user_id,
                    enabled=True,
                    created_by_user_id=created_by_user_id,
                    created_at=now,
                    updated_at=now,
                )
            else:
                row.target_id = target.target_id
                row.bot_user_id = bot_user_id
                row.enabled = True
                row.created_by_user_id = created_by_user_id
                row.updated_at = now
            await stamp_grant_owners(
                session,
                row,
                created_by_user_id=created_by_user_id,
                target_type=target.target_type,
                target_id=target.target_id,
                bot_user_id=bot_user_id,
            )
            session.add(row)
            await session.flush()
            return _grant_view(row)

    async def revoke_target(
        self,
        *,
        plugin_id: str,
        target: NotificationTarget,
    ) -> bool:
        now = datetime.now(UTC)
        async with self._database.sessions() as session, session.begin():
            person_id, space_id = await resolve_grant_target_owners(
                session,
                target_type=target.target_type,
                target_id=target.target_id,
            )
            row = await find_grant_lineage(
                session,
                plugin_id=plugin_id,
                target_type=target.target_type,
                person_id=person_id,
                space_id=space_id,
            )
            if row is None:
                return False
            await require_grant_readable(session, row)
            row.enabled = False
            row.updated_at = now
            outbox_match = (
                PluginNotificationOutboxModel.canonical_target_person_id
                == row.canonical_target_person_id
                if row.canonical_target_person_id
                else PluginNotificationOutboxModel.canonical_target_space_id
                == row.canonical_target_space_id
            )
            turn_match = (
                PluginBackgroundTurnJobModel.canonical_target_person_id
                == row.canonical_target_person_id
                if row.canonical_target_person_id
                else PluginBackgroundTurnJobModel.canonical_target_space_id
                == row.canonical_target_space_id
            )
            await session.execute(
                update(PluginNotificationOutboxModel)
                .where(
                    PluginNotificationOutboxModel.plugin_id == plugin_id,
                    outbox_match,
                    PluginNotificationOutboxModel.status.in_(("pending", "processing")),
                )
                .values(status="cancelled", lease_until=None, updated_at=now)
            )
            await session.execute(
                update(PluginBackgroundTurnJobModel)
                .where(
                    PluginBackgroundTurnJobModel.plugin_id == plugin_id,
                    turn_match,
                    PluginBackgroundTurnJobModel.status.in_(("pending", "processing")),
                )
                .values(status="cancelled", lease_until=None, updated_at=now)
            )
            return True

    async def list_grants(self, plugin_id: str) -> tuple[BackgroundTargetGrantView, ...]:
        async with self._database.sessions() as session:
            rows = (
                await session.scalars(
                    select(PluginBackgroundTargetGrantModel)
                    .where(PluginBackgroundTargetGrantModel.plugin_id == plugin_id)
                    .order_by(
                        PluginBackgroundTargetGrantModel.target_type,
                        PluginBackgroundTargetGrantModel.canonical_target_person_id,
                        PluginBackgroundTargetGrantModel.canonical_target_space_id,
                    )
                )
            ).all()
            for row in rows:
                await require_grant_readable(session, row)
            return tuple(_grant_view(row) for row in rows)

    async def grant_creator(
        self, *, plugin_id: str, target_type: str, target_id: str
    ) -> str | None:
        async with self._database.sessions() as session:
            try:
                person_id, space_id = await resolve_grant_target_owners(
                    session, target_type=target_type, target_id=target_id
                )
                row = await find_grant_lineage(
                    session,
                    plugin_id=plugin_id,
                    target_type=target_type,
                    person_id=person_id,
                    space_id=space_id,
                )
            except PluginOwnershipError:
                return None
            if row is None or not row.enabled:
                return None
            installation = await session.get(PluginInstallationModel, plugin_id)
            if installation is None or not installation.enabled or installation.status != "running":
                return None
            try:
                await require_grant_readable(session, row)
            except PluginOwnershipError:
                return None
            return row.created_by_user_id

    async def publish(
        self,
        *,
        plugin_id: str,
        request: PublishNotificationRequest,
    ) -> NotificationPublishReceipt:
        payload_json = json.dumps(request.payload, ensure_ascii=False, separators=(",", ":"))
        if len(payload_json.encode("utf-8")) > 32 * 1024:
            raise ValueError("notification payload exceeds 32 KiB")
        for attempt in range(2):
            try:
                receipt = await self._publish_once(
                    plugin_id=plugin_id,
                    request=request,
                )
                logger.info(
                    "plugin_external_event_published plugin_id=%s event_type=%s "
                    "target_type=%s event_created=%s deduplicated=%s",
                    plugin_id,
                    request.event_type,
                    request.target.target_type,
                    receipt.event_created,
                    receipt.deduplicated,
                )
                return receipt
            except IntegrityError:
                if attempt:
                    raise
        raise AssertionError("publication retry must return")

    async def _publish_once(
        self,
        *,
        plugin_id: str,
        request: PublishNotificationRequest,
    ) -> NotificationPublishReceipt:
        now = datetime.now(UTC)
        target = request.target
        async with self._database.immediate_session() as session:
            installation = await session.get(PluginInstallationModel, plugin_id)
            if installation is None or not installation.enabled or installation.status != "running":
                raise PluginPermissionError("plugin is not running")
            grant = await _load_enabled_grant(
                session,
                plugin_id=plugin_id,
                target=target,
            )
            if grant is None:
                raise PluginPermissionError("notification target is not granted")
            await require_grant_readable(session, grant)
            if target.target_type == "group":
                scope = ConversationScope.group(grant.bot_user_id, target.target_id)
            else:
                scope = ConversationScope.private(grant.bot_user_id, target.target_id)
            try:
                appended = await self._scoped_events.append_external(
                    scope=scope,
                    platform_message_id=_external_platform_id(plugin_id, request.event_key, target),
                    source_plugin_id=plugin_id,
                    external_source=request.external_source,
                    external_event_key=request.event_key,
                    external_event_type=request.event_type,
                    external_payload=request.payload,
                    external_target_id=target.target_id,
                    content=request.summary,
                    occurred_at=_aware(request.occurred_at),
                    session=session,
                )
            except CanonicalIdentityError as exc:
                raise plugin_ownership_error(exc) from None
            notification_id = _notification_id(
                plugin_id, request.event_key, target.target_type, target.target_id
            )
            for index, handle_id in enumerate(request.media_handles):
                part_key = f"media:{index}:{handle_id}"
                existing_part = await session.scalar(
                    select(PluginNotificationOutboxModel.id).where(
                        PluginNotificationOutboxModel.notification_id == notification_id,
                        PluginNotificationOutboxModel.part_key == part_key,
                    )
                )
                if existing_part is not None:
                    continue
                artifact = await session.get(PluginMediaArtifactModel, handle_id)
                if (
                    artifact is None
                    or artifact.plugin_id != plugin_id
                    or _aware(artifact.expires_at) <= now
                ):
                    raise PluginPermissionError("media handle is invalid, expired, or foreign")
            existing = await session.get(ChatEventModel, appended.event.id)
            if existing is None:
                raise RuntimeError("scoped external event could not be reloaded")
            if existing.author_kind != AuthorKind.SYSTEM.value:
                raise PluginOwnershipError(CANONICAL_OWNER_MISMATCH)
            if existing.author_person_id or existing.author_presence_id:
                raise PluginOwnershipError(CANONICAL_OWNER_MISMATCH)
            event_created = appended.created
            delivery_enqueued = False
            for index, handle_id in enumerate(request.media_handles):
                delivery_enqueued |= await _ensure_outbox_part(
                    session,
                    notification_id=notification_id,
                    part_key=f"media:{index}:{handle_id}",
                    source_event_id=existing.id,
                    plugin_id=plugin_id,
                    grant=grant,
                    event=existing,
                    part_type="media",
                    text="",
                    media_handle_id=handle_id,
                    now=now,
                )
            if request.text:
                delivery_enqueued |= await _ensure_outbox_part(
                    session,
                    notification_id=notification_id,
                    part_key="text",
                    source_event_id=existing.id,
                    plugin_id=plugin_id,
                    grant=grant,
                    event=existing,
                    part_type="text",
                    text=request.text,
                    media_handle_id=None,
                    now=now,
                )
            job_created = False
            if request.ask_agent:
                job = await session.scalar(
                    select(PluginBackgroundTurnJobModel).where(
                        PluginBackgroundTurnJobModel.source_event_id == existing.id
                    )
                )
                if job is None:
                    job = PluginBackgroundTurnJobModel(
                        source_event_id=existing.id,
                        plugin_id=plugin_id,
                        target_type=grant.target_type,
                        target_id=target.target_id,
                        bot_user_id=grant.bot_user_id,
                        agent_intent=request.agent_intent,
                        status="pending",
                        attempts=0,
                        max_attempts=3,
                        next_attempt_at=now,
                        lease_until=None,
                        generated_text="",
                        tool_calls_used=0,
                        model_requests=0,
                        last_error_category=None,
                        created_at=now,
                        updated_at=now,
                        completed_at=None,
                    )
                    await _stamp_publication_child(
                        session,
                        job,
                        grant=grant,
                        event=existing,
                    )
                    session.add(job)
                    await session.flush()
                    job_created = True
            receipt = NotificationPublishReceipt(
                notification_id=notification_id,
                source_event_id=existing.id,
                event_created=event_created,
                delivery_enqueued=delivery_enqueued,
                agent_turn_enqueued=job_created,
                deduplicated=not event_created,
            )
        return receipt

    async def claim_outbox(
        self,
        *,
        lease_seconds: int = OUTBOX_LEASE_SECONDS,
    ) -> OutboxRecord | None:
        now = datetime.now(UTC)
        async with self._database.immediate_session() as session:
            row = await session.scalar(
                select(PluginNotificationOutboxModel)
                .where(
                    PluginNotificationOutboxModel.next_attempt_at <= now,
                    or_(
                        PluginNotificationOutboxModel.status == "pending",
                        (
                            (PluginNotificationOutboxModel.status == "processing")
                            & (PluginNotificationOutboxModel.lease_until < now)
                        ),
                    ),
                )
                .order_by(
                    PluginNotificationOutboxModel.created_at, PluginNotificationOutboxModel.id
                )
                .limit(1)
            )
            if row is None:
                return None
            row.status = "processing"
            row.attempts += 1
            row.lease_until = now + timedelta(seconds=lease_seconds)
            row.updated_at = now
            await session.flush()
            try:
                await require_queued_work_readable(session, row)
            except PluginOwnershipError as exc:
                row.status = "failed"
                row.last_error_category = queued_work_error_category(exc, row)
                row.lease_until = None
                return None
            return _outbox_record(row)

    async def finish_outbox(
        self,
        item_id: int,
        *,
        attempt: int,
        status: str,
        platform_message_id: str | None = None,
        error_category: str | None = None,
    ) -> bool:
        now = datetime.now(UTC)
        async with self._database.immediate_session() as session:
            row = await session.get(PluginNotificationOutboxModel, item_id)
            if not _owns_processing_attempt(row, attempt=attempt):
                return False
            assert row is not None
            row.status = status
            row.platform_message_id = platform_message_id
            row.last_error_category = error_category
            row.lease_until = None
            row.updated_at = now
            row.sent_at = now if status == "sent" else None
            return True

    async def retry_outbox(
        self,
        item_id: int,
        *,
        attempt: int | None = None,
        error_category: str,
        session: AsyncSession | None = None,
    ) -> bool:
        now = datetime.now(UTC)

        async def mutate(active: AsyncSession) -> bool:
            row = await active.get(PluginNotificationOutboxModel, item_id)
            if attempt is not None and not _owns_processing_attempt(row, attempt=attempt):
                return False
            if row is None:
                return False
            # Historical terminal rows may legitimately lack canonical owners.
            # They remain auditable, but can never re-enter the live queue by
            # treating raw target provenance as routing authority.
            await require_queued_work_readable(active, row)
            if row.attempts >= row.max_attempts:
                row.status = "failed"
            else:
                delays = (10, 30, 120, 600, 1800)
                row.status = "pending"
                row.next_attempt_at = now + timedelta(seconds=delays[min(row.attempts - 1, 4)])
            row.last_error_category = error_category
            row.lease_until = None
            row.updated_at = now
            return True

        if session is not None:
            return await mutate(session)
        async with self._database.immediate_session() as active:
            return await mutate(active)

    async def claim_turn(
        self, *, lease_seconds: int = TURN_LEASE_SECONDS
    ) -> BackgroundTurnJobRecord | None:
        now = datetime.now(UTC)
        async with self._database.immediate_session() as session:
            row = await session.scalar(
                select(PluginBackgroundTurnJobModel)
                .where(
                    PluginBackgroundTurnJobModel.next_attempt_at <= now,
                    or_(
                        PluginBackgroundTurnJobModel.status == "pending",
                        (
                            (PluginBackgroundTurnJobModel.status == "processing")
                            & (PluginBackgroundTurnJobModel.lease_until < now)
                        ),
                    ),
                )
                .order_by(PluginBackgroundTurnJobModel.created_at, PluginBackgroundTurnJobModel.id)
                .limit(1)
            )
            if row is None:
                return None
            row.status = "processing"
            row.attempts += 1
            row.lease_until = now + timedelta(seconds=lease_seconds)
            row.updated_at = now
            await session.flush()
            try:
                await require_queued_work_readable(session, row)
            except PluginOwnershipError as exc:
                row.status = "failed"
                row.last_error_category = queued_work_error_category(exc, row)
                row.lease_until = None
                return None
            conversation = await session.get(
                CanonicalConversationModel,
                row.canonical_conversation_id,
            )
            if conversation is None:
                row.status = "cancelled"
                row.last_error_category = TURN_ERROR_SUPERSEDED_COVERED
                row.lease_until = None
                return None
            generation = int(conversation.generation)
            category = await _turn_fence_category(
                session,
                row,
                expected_generation=generation,
                include_coverage=True,
            )
            if category is not None:
                _cancel_turn(row, category=category, now=now)
                return None
            return _turn_record(row, generation=generation)

    async def validate_turn_attempt(
        self,
        job_id: int,
        *,
        attempt: int,
        generation: int,
        lease_seconds: int = TURN_LEASE_SECONDS,
    ) -> None:
        """Fence and renew the exact attempt immediately before each model request."""

        now = datetime.now(UTC)
        category: str | None = None
        async with self._database.immediate_session() as session:
            job = await session.get(PluginBackgroundTurnJobModel, job_id)
            if not _owns_processing_attempt(job, attempt=attempt):
                category = TURN_ERROR_ATTEMPT_RECLAIMED
            else:
                assert job is not None
                category = await _turn_fence_category(
                    session,
                    job,
                    expected_generation=generation,
                    include_coverage=True,
                )
                if category is None:
                    job.lease_until = now + timedelta(seconds=max(1, lease_seconds))
                    job.updated_at = now
                else:
                    _cancel_turn(job, category=category, now=now)
        if category is not None:
            raise BackgroundTurnFenceError(category)

    async def finish_turn(
        self,
        job_id: int,
        *,
        attempt: int,
        generation: int,
        text: str,
        tool_calls_used: int,
        model_requests: int,
    ) -> bool:
        now = datetime.now(UTC)
        async with self._database.immediate_session() as session:
            job = await session.get(PluginBackgroundTurnJobModel, job_id)
            if not _owns_processing_attempt(job, attempt=attempt):
                return False
            assert job is not None
            category = await _turn_fence_category(
                session,
                job,
                expected_generation=generation,
                include_coverage=True,
            )
            if category is not None:
                _cancel_turn(job, category=category, now=now)
                return False
            job.status = "completed"
            job.generated_text = text[:24_000]
            job.tool_calls_used = tool_calls_used
            job.model_requests = model_requests
            job.lease_until = None
            job.updated_at = now
            job.completed_at = now
            if text.strip():
                existing = await session.scalar(
                    select(PluginNotificationOutboxModel).where(
                        PluginNotificationOutboxModel.source_event_id == job.source_event_id,
                        PluginNotificationOutboxModel.part_key == "agent_reply",
                    )
                )
                if existing is None:
                    notification_id = await session.scalar(
                        select(PluginNotificationOutboxModel.notification_id)
                        .where(PluginNotificationOutboxModel.source_event_id == job.source_event_id)
                        .limit(1)
                    ) or _notification_id(
                        job.plugin_id,
                        f"source:{job.source_event_id}",
                        job.target_type,
                        job.canonical_target_person_id or job.canonical_target_space_id or "",
                    )
                    reply = PluginNotificationOutboxModel(
                        notification_id=notification_id,
                        part_key="agent_reply",
                        source_event_id=job.source_event_id,
                        plugin_id=job.plugin_id,
                        target_type=job.target_type,
                        target_id=job.target_id,
                        bot_user_id=job.bot_user_id,
                        part_type="agent_reply",
                        text=text[:12_000],
                        media_handle_id=None,
                        status="pending",
                        attempts=0,
                        max_attempts=5,
                        next_attempt_at=now,
                        lease_until=None,
                        platform_message_id=None,
                        last_error_category=None,
                        created_at=now,
                        updated_at=now,
                        sent_at=None,
                    )
                    event = await session.get(ChatEventModel, job.source_event_id)
                    inherit_queued_canonicals(reply, parent=job, event=event)
                    await require_queued_work_readable(session, reply)
                    session.add(reply)
            return True

    async def fail_turn(self, job_id: int, *, attempt: int, error_category: str) -> bool:
        now = datetime.now(UTC)
        async with self._database.immediate_session() as session:
            row = await session.get(PluginBackgroundTurnJobModel, job_id)
            if not _owns_processing_attempt(row, attempt=attempt):
                return False
            assert row is not None
            if row.attempts >= row.max_attempts:
                row.status = "failed"
            else:
                delays = (30, 120, 600)
                row.status = "pending"
                row.next_attempt_at = now + timedelta(
                    seconds=delays[min(max(row.attempts - 1, 0), len(delays) - 1)]
                )
            row.last_error_category = error_category
            row.lease_until = None
            row.updated_at = now
            return True

    async def abandon_turn(self, job_id: int, *, attempt: int, error_category: str) -> bool:
        """Permanently stop a background turn that is unsafe to repeat."""

        now = datetime.now(UTC)
        async with self._database.immediate_session() as session:
            row = await session.get(PluginBackgroundTurnJobModel, job_id)
            if not _owns_processing_attempt(row, attempt=attempt):
                return False
            assert row is not None
            row.status = "failed"
            row.last_error_category = error_category
            row.lease_until = None
            row.updated_at = now
            return True

    async def defer_turn(
        self,
        job_id: int,
        *,
        attempt: int,
        error_category: str,
        delay_seconds: int = 5,
        preserve_budget: bool = False,
    ) -> bool:
        """Return an unstarted/interrupted turn to the queue without losing its lease."""

        now = datetime.now(UTC)
        async with self._database.immediate_session() as session:
            row = await session.get(PluginBackgroundTurnJobModel, job_id)
            if not _owns_processing_attempt(row, attempt=attempt):
                return False
            assert row is not None
            row.status = "pending"
            if preserve_budget:
                row.max_attempts += 1
            row.next_attempt_at = now + timedelta(seconds=max(1, delay_seconds))
            row.last_error_category = error_category
            row.lease_until = None
            row.updated_at = now
            return True

    async def counts(self, plugin_id: str) -> dict[str, int]:
        async with self._database.sessions() as session:
            outbox = (
                (
                    await session.execute(
                        select(PluginNotificationOutboxModel.status).where(
                            PluginNotificationOutboxModel.plugin_id == plugin_id
                        )
                    )
                )
                .scalars()
                .all()
            )
            turns = (
                (
                    await session.execute(
                        select(PluginBackgroundTurnJobModel.status).where(
                            PluginBackgroundTurnJobModel.plugin_id == plugin_id
                        )
                    )
                )
                .scalars()
                .all()
            )
        result: dict[str, int] = {}
        for prefix, values in (("outbox", outbox), ("turn", turns)):
            for value in values:
                key = f"{prefix}_{value}"
                result[key] = result.get(key, 0) + 1
        return result

    async def require_outbox_ready(self, item: OutboxRecord) -> None:
        """Require the exact attempt plus live target, Conversation, and Presence."""

        now = datetime.now(UTC)
        async with self._database.immediate_session() as session:
            row = await session.get(PluginNotificationOutboxModel, item.id)
            if not _owns_processing_attempt(row, attempt=item.attempts):
                raise BackgroundTurnFenceError(TURN_ERROR_ATTEMPT_RECLAIMED)
            assert row is not None
            await require_queued_work_readable(session, row)
            row.lease_until = now + timedelta(seconds=OUTBOX_LEASE_SECONDS)
            row.updated_at = now

    async def require_agent_reply_ready(self, item: OutboxRecord) -> str:
        """Return the frozen effect-gate key after an exact delivery-time fence."""

        from qq_ai_bot.conversation.hydrate import require_primary_alias_for_conversation

        now = datetime.now(UTC)
        category: str | None = None
        primary_alias: str | None = None
        async with self._database.immediate_session() as session:
            row = await session.get(PluginNotificationOutboxModel, item.id)
            if not _owns_processing_attempt(row, attempt=item.attempts):
                category = TURN_ERROR_ATTEMPT_RECLAIMED
            else:
                assert row is not None
                try:
                    await require_queued_work_readable(session, row)
                except PluginOwnershipError as exc:
                    category = queued_work_error_category(exc, row)
                conversation = await session.get(
                    CanonicalConversationModel,
                    row.canonical_conversation_id,
                )
                source = await session.get(ChatEventModel, row.source_event_id)
                if category is not None:
                    pass
                elif conversation is None or source is None:
                    category = TURN_ERROR_GENERATION_STALE
                elif (
                    source.event_kind != "external_event"
                    or source.direction != "external"
                    or source.author_kind != AuthorKind.SYSTEM.value
                    or source.source_plugin_id != row.plugin_id
                    or source.canonical_conversation_id != conversation.id
                    or source.id <= conversation.starts_after_event_id
                    or source.id > conversation.last_event_id
                    or not suppression_is_canonical_live(source.suppression_status)
                ):
                    category = TURN_ERROR_GENERATION_STALE
                elif await _has_later_human_message(
                    session,
                    source=source,
                    through_event_id=int(conversation.last_event_id),
                ):
                    category = TURN_ERROR_LATER_HUMAN
                else:
                    try:
                        primary_alias = await require_primary_alias_for_conversation(
                            session,
                            conversation.id,
                        )
                    except CanonicalIdentityError:
                        category = TURN_ERROR_GENERATION_STALE
                    if category is None:
                        row.lease_until = now + timedelta(seconds=OUTBOX_LEASE_SECONDS)
                        row.updated_at = now
            if (
                category is not None
                and row is not None
                and _owns_processing_attempt(
                    row,
                    attempt=item.attempts,
                )
            ):
                row.status = "cancelled"
                row.last_error_category = category
                row.lease_until = None
                row.updated_at = now
        if category is not None or primary_alias is None:
            raise BackgroundTurnFenceError(category or TURN_ERROR_SUPERSEDED_COVERED)
        return primary_alias

    async def granted_canonical_creator(
        self,
        *,
        plugin_id: str,
        person_id: str | None,
        space_id: str | None,
    ) -> str | None:
        """Read a grant by persisted Person XOR Space. No raw QQ/group key."""

        async with self._database.sessions() as session:
            try:
                grant = await _canonical_enabled_grant(
                    session,
                    plugin_id=plugin_id,
                    person_id=person_id,
                    space_id=space_id,
                )
            except PluginOwnershipError:
                return None
            if grant is None:
                return None
            installation = await session.get(PluginInstallationModel, plugin_id)
            if installation is None or not installation.enabled or installation.status != "running":
                return None
            return grant.canonical_created_by_person_id

    async def load_background_context(self, job: BackgroundTurnJobRecord) -> QueuedCanonicalContext:
        """Load live queued Conversation + grant creator. Never re-resolve from raw keys."""

        from qq_ai_bot.conversation.hydrate import (
            require_primary_alias_for_conversation,
            synthetic_scope_id,
        )

        now = datetime.now(UTC)
        category: str | None = None
        result: QueuedCanonicalContext | None = None
        async with self._database.immediate_session() as session:
            stored = await session.get(PluginBackgroundTurnJobModel, job.id)
            if not _owns_processing_attempt(stored, attempt=job.attempts):
                category = TURN_ERROR_ATTEMPT_RECLAIMED
            else:
                assert stored is not None
                category = await _turn_fence_category(
                    session,
                    stored,
                    expected_generation=job.generation,
                    include_coverage=True,
                )
                if category is not None:
                    _cancel_turn(stored, category=category, now=now)
            if category is None:
                assert stored is not None
                await require_queued_work_readable(session, stored)
                grant = await _canonical_enabled_grant(
                    session,
                    plugin_id=stored.plugin_id,
                    person_id=stored.canonical_target_person_id,
                    space_id=stored.canonical_target_space_id,
                )
                if grant is None:
                    raise PluginOwnershipError(MISSING_CANONICAL_OWNER)
                conversation = await require_live_conversation(
                    session,
                    stored.canonical_conversation_id,
                )
                try:
                    primary = await require_primary_alias_for_conversation(
                        session,
                        conversation.id,
                    )
                except CanonicalIdentityError as exc:
                    raise plugin_ownership_error(exc) from None
                creator_id = grant.canonical_created_by_person_id
                if not creator_id:
                    raise PluginOwnershipError(MISSING_CANONICAL_OWNER)
                await require_live_person(session, creator_id)
                result = QueuedCanonicalContext(
                    person_id=stored.canonical_target_person_id,
                    space_id=stored.canonical_target_space_id,
                    conversation_id=conversation.id,
                    provenance_presence_id=stored.canonical_presence_id,
                    creator_person_id=creator_id,
                    primary_alias=primary,
                    generation=int(conversation.generation),
                    scope_id=synthetic_scope_id(conversation.id),
                )
        if category is not None or result is None:
            raise BackgroundTurnFenceError(category or TURN_ERROR_SUPERSEDED_COVERED)
        return result

    async def ensure_resolved_transport_alias(
        self,
        *,
        conversation_id: str,
        transport_key: str,
    ) -> None:
        """Attach the current Presence key as a secondary alias. Primary stays frozen."""

        from qq_ai_bot.conversation.hydrate import ensure_legacy_alias

        async with self._database.sessions() as session, session.begin():
            conversation = await require_live_conversation(session, conversation_id)
            try:
                await ensure_legacy_alias(
                    session,
                    conversation_id=conversation.id,
                    scope_key=transport_key,
                    primary=False,
                )
            except CanonicalIdentityError as exc:
                raise plugin_ownership_error(exc) from None

    async def conversation_watermark(self, conversation_id: str) -> tuple[int, str] | None:
        """Return frozen (generation, primary alias) for one queued Conversation."""

        from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel
        from qq_ai_bot.conversation.hydrate import require_primary_alias_for_conversation

        if not conversation_id:
            return None
        async with self._database.sessions() as session:
            conversation = await session.get(CanonicalConversationModel, conversation_id)
            if conversation is None:
                return None
            try:
                primary = await require_primary_alias_for_conversation(session, conversation.id)
            except CanonicalIdentityError:
                return None
            return int(conversation.generation), primary


def inherit_queued_canonicals(
    child: Any,
    *,
    parent: object,
    event: ChatEventModel | None = None,
) -> None:
    """Copy persisted job/event canonicals. Never recompute from raw target/bot keys."""

    person_id = getattr(parent, "canonical_target_person_id", None)
    space_id = getattr(parent, "canonical_target_space_id", None)
    conversation_id = getattr(parent, "canonical_conversation_id", None)
    presence_id = getattr(parent, "canonical_presence_id", None)
    if event is not None:
        if not conversation_id:
            conversation_id = event.canonical_conversation_id
        if not presence_id:
            presence_id = event.ingress_presence_id
    child.canonical_target_person_id = person_id
    child.canonical_target_space_id = space_id
    child.canonical_conversation_id = conversation_id
    child.canonical_presence_id = presence_id


def queued_work_error_category(exc: PluginOwnershipError, row: object) -> str:
    """Sanitize claim/read failures. Dual/missing XOR keep stable delivery names."""

    person_id = getattr(row, "canonical_target_person_id", None)
    space_id = getattr(row, "canonical_target_space_id", None)
    if exc.category == MISSING_CANONICAL_OWNER and not person_id and not space_id:
        return "canonical_target_missing"
    if exc.category == STATE_MISMATCH and person_id and space_id:
        return "canonical_target_ambiguous"
    return exc.category


def _owns_processing_attempt(row: object | None, *, attempt: int) -> bool:
    return bool(
        row is not None
        and getattr(row, "status", None) == "processing"
        and int(getattr(row, "attempts", -1)) == attempt
    )


def _cancel_turn(
    row: PluginBackgroundTurnJobModel,
    *,
    category: str,
    now: datetime,
) -> None:
    row.status = "cancelled"
    row.last_error_category = category
    row.lease_until = None
    row.updated_at = now


async def _has_later_human_message(
    session: AsyncSession,
    *,
    source: ChatEventModel,
    through_event_id: int,
) -> bool:
    if not source.canonical_conversation_id:
        return True
    later = await session.scalar(
        select(ChatEventModel.id)
        .where(
            ChatEventModel.canonical_conversation_id == source.canonical_conversation_id,
            ChatEventModel.id > source.id,
            ChatEventModel.id <= through_event_id,
            keeper_event_clause(),
            ChatEventModel.event_kind == "message",
            ChatEventModel.direction == "inbound",
            ChatEventModel.author_kind == AuthorKind.PERSON.value,
        )
        .order_by(ChatEventModel.id.asc())
        .limit(1)
    )
    return later is not None


async def _turn_fence_category(
    session: AsyncSession,
    job: PluginBackgroundTurnJobModel,
    *,
    expected_generation: int,
    include_coverage: bool,
) -> str | None:
    conversation = await session.get(
        CanonicalConversationModel,
        job.canonical_conversation_id,
    )
    source = await session.get(ChatEventModel, job.source_event_id)
    if conversation is None or source is None:
        return TURN_ERROR_SUPERSEDED_COVERED
    if int(conversation.generation) != expected_generation:
        return TURN_ERROR_SUPERSEDED_COVERED
    if (
        source.event_kind != "external_event"
        or source.direction != "external"
        or source.author_kind != AuthorKind.SYSTEM.value
        or source.source_plugin_id != job.plugin_id
        or source.canonical_conversation_id != conversation.id
        or source.id <= conversation.starts_after_event_id
        or source.id > conversation.last_event_id
        or not suppression_is_canonical_live(source.suppression_status)
    ):
        return TURN_ERROR_SUPERSEDED_COVERED
    if include_coverage:
        semantic = await session.get(CanonicalConversationRollupModel, conversation.id)
        overlay = await session.get(
            CanonicalConversationRollupEmergencyOverlayModel,
            conversation.id,
        )
        coverage = session_effective_coverage(
            generation=int(conversation.generation),
            starts_after=int(conversation.starts_after_event_id),
            last_event_id=int(conversation.last_event_id),
            overlay=overlay,
            semantic=semantic,
        )
        if source.id <= coverage:
            return TURN_ERROR_SUPERSEDED_COVERED
    if await _has_later_human_message(
        session,
        source=source,
        through_event_id=int(conversation.last_event_id),
    ):
        return TURN_ERROR_LATER_HUMAN
    return None


async def require_queued_work_readable(session: AsyncSession, row: object) -> None:
    """Require live Person XOR Space, Conversation, and optional Presence."""

    person_id = getattr(row, "canonical_target_person_id", None)
    space_id = getattr(row, "canonical_target_space_id", None)
    if person_id and space_id:
        raise PluginOwnershipError(STATE_MISMATCH)
    if not person_id and not space_id:
        raise PluginOwnershipError(MISSING_CANONICAL_OWNER)
    target_type = getattr(row, "target_type", None)
    if target_type == "private":
        if space_id:
            raise PluginOwnershipError(STATE_MISMATCH)
        await require_live_person(session, person_id)
    elif target_type == "group":
        if person_id:
            raise PluginOwnershipError(STATE_MISMATCH)
        await require_live_space(session, space_id)
    else:
        raise PluginOwnershipError(STATE_MISMATCH)
    conversation = await require_live_conversation(
        session, getattr(row, "canonical_conversation_id", None)
    )
    if person_id:
        if conversation.person_id != person_id or conversation.space_id:
            raise PluginOwnershipError(CANONICAL_OWNER_MISMATCH)
    elif conversation.space_id != space_id or conversation.person_id:
        raise PluginOwnershipError(CANONICAL_OWNER_MISMATCH)
    await require_presence_provenance(
        session,
        getattr(row, "canonical_presence_id", None),
    )


async def _canonical_enabled_grant(
    session: AsyncSession,
    *,
    plugin_id: str,
    person_id: str | None,
    space_id: str | None,
) -> PluginBackgroundTargetGrantModel | None:
    if person_id and space_id:
        raise PluginOwnershipError(STATE_MISMATCH)
    if not person_id and not space_id:
        raise PluginOwnershipError(MISSING_CANONICAL_OWNER)
    if person_id:
        rows = list(
            (
                await session.scalars(
                    select(PluginBackgroundTargetGrantModel).where(
                        PluginBackgroundTargetGrantModel.plugin_id == plugin_id,
                        PluginBackgroundTargetGrantModel.canonical_target_person_id == person_id,
                        PluginBackgroundTargetGrantModel.enabled.is_(True),
                    )
                )
            ).all()
        )
    else:
        rows = list(
            (
                await session.scalars(
                    select(PluginBackgroundTargetGrantModel).where(
                        PluginBackgroundTargetGrantModel.plugin_id == plugin_id,
                        PluginBackgroundTargetGrantModel.canonical_target_space_id == space_id,
                        PluginBackgroundTargetGrantModel.enabled.is_(True),
                    )
                )
            ).all()
        )
    if len(rows) > 1:
        raise PluginOwnershipError(STATE_MISMATCH)
    if not rows:
        return None
    await require_grant_readable(session, rows[0])
    return rows[0]


async def _ensure_outbox_part(
    session: AsyncSession,
    *,
    notification_id: str,
    part_key: str,
    source_event_id: int,
    plugin_id: str,
    grant: PluginBackgroundTargetGrantModel,
    event: ChatEventModel,
    part_type: str,
    text: str,
    media_handle_id: str | None,
    now: datetime,
) -> bool:
    existing = await session.scalar(
        select(PluginNotificationOutboxModel).where(
            PluginNotificationOutboxModel.notification_id == notification_id,
            PluginNotificationOutboxModel.part_key == part_key,
        )
    )
    if existing is not None:
        return False
    row = PluginNotificationOutboxModel(
        notification_id=notification_id,
        part_key=part_key,
        source_event_id=source_event_id,
        plugin_id=plugin_id,
        target_type=grant.target_type,
        target_id=grant.target_id,
        bot_user_id=grant.bot_user_id,
        part_type=part_type,
        text=text,
        media_handle_id=media_handle_id,
        status="pending",
        attempts=0,
        max_attempts=5,
        next_attempt_at=now,
        lease_until=None,
        platform_message_id=None,
        last_error_category=None,
        created_at=now,
        updated_at=now,
        sent_at=None,
    )
    await _stamp_publication_child(
        session,
        row,
        grant=grant,
        event=event,
    )
    session.add(row)
    await session.flush()
    return True


async def _stamp_publication_child(
    session: AsyncSession,
    row: object,
    *,
    grant: PluginBackgroundTargetGrantModel,
    event: ChatEventModel,
) -> None:
    inherit_publication_canonicals(
        row,
        grant=grant,
        conversation_id=event.canonical_conversation_id,
        presence_id=event.ingress_presence_id,
    )
    await require_inherited_publication(
        session,
        row,
        grant=grant,
        conversation_id=event.canonical_conversation_id,
        presence_id=event.ingress_presence_id,
    )


async def _grant_owners(
    session: AsyncSession,
    *,
    created_by_user_id: str,
    target: NotificationTarget,
) -> tuple[str | None, str | None]:
    try:
        await resolve_human_person_id(
            session,
            created_by_user_id,
            missing_message="grant creator is not a known person",
        )
    except PluginOwnershipError as exc:
        raise _grant_api_error(exc, creator=True, target=target) from None
    try:
        return await resolve_grant_target_owners(
            session,
            target_type=target.target_type,
            target_id=target.target_id,
        )
    except PluginOwnershipError as exc:
        raise _grant_api_error(exc, creator=False, target=target) from None


def _grant_api_error(
    exc: PluginOwnershipError,
    *,
    creator: bool,
    target: NotificationTarget,
) -> PluginPermissionError | PluginOwnershipError:
    if exc.category not in {MISSING_CANONICAL_OWNER, CANONICAL_OWNER_DISABLED}:
        return exc
    if creator:
        return PluginPermissionError("grant creator is not a known person")
    if target.target_type == "group":
        return PluginPermissionError("notification group is unknown or disabled")
    return PluginPermissionError("notification private target is unknown")


async def _load_enabled_grant(
    session: AsyncSession,
    *,
    plugin_id: str,
    target: NotificationTarget,
) -> PluginBackgroundTargetGrantModel | None:
    person_id, space_id = await resolve_grant_target_owners(
        session,
        target_type=target.target_type,
        target_id=target.target_id,
    )
    row = await find_grant_lineage(
        session,
        plugin_id=plugin_id,
        target_type=target.target_type,
        person_id=person_id,
        space_id=space_id,
    )
    if row is None or not row.enabled:
        return None
    return row


def _grant_view(row: PluginBackgroundTargetGrantModel) -> BackgroundTargetGrantView:
    return BackgroundTargetGrantView(
        target_type=row.target_type,
        target_id=row.target_id,
        bot_user_id=row.bot_user_id,
        enabled=row.enabled,
        created_by_user_id=row.created_by_user_id,
    )


def _outbox_record(row: PluginNotificationOutboxModel) -> OutboxRecord:
    return OutboxRecord(
        id=row.id,
        notification_id=row.notification_id,
        source_event_id=row.source_event_id,
        plugin_id=row.plugin_id,
        target_type=row.target_type,
        target_id=row.target_id,
        bot_user_id=row.bot_user_id,
        part_type=row.part_type,
        text=row.text,
        media_handle_id=row.media_handle_id,
        attempts=row.attempts,
        canonical_target_person_id=row.canonical_target_person_id,
        canonical_target_space_id=row.canonical_target_space_id,
        canonical_presence_id=row.canonical_presence_id,
        canonical_conversation_id=row.canonical_conversation_id,
    )


def _turn_record(
    row: PluginBackgroundTurnJobModel,
    *,
    generation: int,
) -> BackgroundTurnJobRecord:
    return BackgroundTurnJobRecord(
        id=row.id,
        source_event_id=row.source_event_id,
        plugin_id=row.plugin_id,
        target_type=row.target_type,
        target_id=row.target_id,
        bot_user_id=row.bot_user_id,
        agent_intent=row.agent_intent,
        attempts=row.attempts,
        generation=generation,
        canonical_target_person_id=row.canonical_target_person_id,
        canonical_target_space_id=row.canonical_target_space_id,
        canonical_presence_id=row.canonical_presence_id,
        canonical_conversation_id=row.canonical_conversation_id,
    )


def _notification_id(plugin_id: str, event_key: str, target_type: str, target_id: str) -> str:
    raw = f"{plugin_id}\0{event_key}\0{target_type}\0{target_id}".encode()
    return hashlib.sha256(raw).hexdigest()


def _external_platform_id(plugin_id: str, event_key: str, target: NotificationTarget) -> str:
    digest = _notification_id(plugin_id, event_key, target.target_type, target.target_id)
    return f"external-{digest}"[:128]


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
