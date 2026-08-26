"""Short SQLite transactions and CAS operations for conversation rollup."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import cast

from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from qq_ai_bot.conversation.canonical_db_models import (
    CanonicalConversationModel,
    CanonicalConversationRollupEmergencyOverlayModel,
    CanonicalConversationRollupJobModel,
    CanonicalConversationRollupModel,
    ConversationLegacyAliasModel,
)
from qq_ai_bot.conversation.hydrate import (
    hydrate_scope_state_from_canonical,
    synthetic_scope_id,
)
from qq_ai_bot.conversation.rollup.coverage import (
    session_effective_coverage,
    valid_same_generation_overlay,
)
from qq_ai_bot.conversation.rollup.errors import (
    ConversationCoverageError,
    RollupLeaseLostError,
    RollupSourceChangedError,
)
from qq_ai_bot.conversation.rollup.metrics import ConversationRollupMetrics
from qq_ai_bot.conversation.rollup.models import (
    LLM_ORIGIN_INELIGIBLE,
    POLICY_PARK_DELAY,
    ConversationPromptSnapshot,
    ConversationRollupDetailedStatus,
    ConversationRollupState,
    ConversationScopeState,
    EmergencyOverlayDisposition,
    RollupCandidate,
    RollupCheckpointStatus,
    RollupCommitResult,
    RollupJobClaim,
    RollupJobMetadata,
    RollupKind,
    RollupPolicyConfig,
)
from qq_ai_bot.conversation.rollup.prompt_accounting import (
    durable_uncovered_characters,
    is_prompt_visible_message,
    prompt_accounting_characters,
    prompt_visible_event_count,
    source_accounting_characters,
)
from qq_ai_bot.conversation.rollup.renderer import (
    serialize_compaction_source_events,
    source_fingerprint,
)
from qq_ai_bot.domain.conversations import ConversationScope
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.models import ChatEventModel
from qq_ai_bot.persistence.repository_helpers import _event_record, keeper_event_clause
from qq_ai_bot.persistence.repository_records import EventRecord


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _as_utc_iso(value: datetime | None) -> str | None:
    normalized = _as_utc(value)
    return normalized.isoformat() if normalized is not None else None


def _age_seconds(value: datetime | None, now: datetime) -> int:
    normalized = _as_utc(value)
    return max(0, int((now - normalized).total_seconds())) if normalized is not None else 0


_EMERGENCY_OVERLAY_RETRY_SECONDS = 15
_DEFAULT_RETRY_MAX_SECONDS = 960


def _overlay_backoff_seconds(failure_count: int, retry_max_seconds: int) -> int:
    return int(min(retry_max_seconds, 15 * (2 ** min(max(failure_count, 1) - 1, 20))))


def _overlay_should_replace(
    overlay: CanonicalConversationRollupEmergencyOverlayModel | None,
    *,
    valid: bool,
    covered_through: int,
) -> bool:
    if not valid or overlay is None:
        return True
    return covered_through >= overlay.covered_through_event_id


def _canonical_overlay_state(
    row: CanonicalConversationRollupEmergencyOverlayModel, scope_id: int
) -> ConversationRollupState:
    return ConversationRollupState(
        scope_id=scope_id,
        generation=row.generation,
        covered_through_event_id=row.covered_through_event_id,
        summary_text=row.summary_text,
        summary_kind=RollupKind.EMERGENCY,
        source_fingerprint=row.source_fingerprint,
        revision=row.revision,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


async def _reconcile_overlay_with_semantic(
    session: AsyncSession,
    overlay: CanonicalConversationRollupEmergencyOverlayModel | None,
    *,
    generation: int,
    covered_through: int,
    next_semantic_revision: int,
) -> bool:
    """Keep overlay effective until semantic coverage catches it. Return True if still ahead."""

    if overlay is None:
        return False
    if overlay.generation != generation:
        await session.delete(overlay)
        return False
    if covered_through >= overlay.covered_through_event_id:
        await session.delete(overlay)
        return False
    overlay.base_semantic_revision = next_semantic_revision
    return True


def _valid_same_generation_overlay(
    overlay: CanonicalConversationRollupEmergencyOverlayModel | None,
    *,
    generation: int,
    starts_after: int,
    last_event_id: int,
    semantic_revision: int,
) -> bool:
    return valid_same_generation_overlay(
        overlay,
        generation=generation,
        starts_after=starts_after,
        last_event_id=last_event_id,
        semantic_revision=semantic_revision,
    )


def _source_checkpoint(
    *,
    emergency: bool,
    overlay: CanonicalConversationRollupEmergencyOverlayModel | None,
    semantic: CanonicalConversationRollupModel | None,
    generation: int,
    starts_after: int,
    last_event_id: int,
) -> tuple[int, int, str]:
    semantic_revision = semantic.revision if semantic is not None else 0
    if emergency and _valid_same_generation_overlay(
        overlay,
        generation=generation,
        starts_after=starts_after,
        last_event_id=last_event_id,
        semantic_revision=semantic_revision,
    ):
        assert overlay is not None
        return overlay.covered_through_event_id, overlay.revision, overlay.summary_text
    if semantic is None:
        return starts_after, 0, ""
    return semantic.covered_through_event_id, semantic.revision, semantic.summary_text


def _sanitize_overlay_error_category(category: str | None) -> str | None:
    if category is None:
        return None
    token = str(category).strip().splitlines()[0].split(":", 1)[0].split()[0]
    token = token[:64]
    return token or None


def _dispose_job_after_overlay(
    job: CanonicalConversationRollupJobModel,
    claim: RollupJobClaim,
    now: datetime,
    *,
    disposition: EmergencyOverlayDisposition,
    error_category: str | None,
    retry_max_seconds: int,
) -> None:
    signal_changed = job.signal_revision != claim.claimed_signal_revision
    sanitized = _sanitize_overlay_error_category(error_category)
    job.status = "pending"
    job.lease_owner = None
    job.lease_token = None
    job.lease_until = None
    job.updated_at = now
    if disposition is EmergencyOverlayDisposition.POLICY:
        job.last_error_category = sanitized or LLM_ORIGIN_INELIGIBLE
        job.next_attempt_at = now if signal_changed else now + POLICY_PARK_DELAY
        return
    if disposition is EmergencyOverlayDisposition.MODEL_FAILURE:
        job.failure_count = job.failure_count + 1
        job.last_error_category = sanitized
        job.next_attempt_at = now + timedelta(
            seconds=_overlay_backoff_seconds(job.failure_count, retry_max_seconds)
        )
        return
    job.last_error_category = sanitized
    delay = 0 if signal_changed else _EMERGENCY_OVERLAY_RETRY_SECONDS
    job.next_attempt_at = now + timedelta(seconds=delay)


def _prompt_kwargs(config: RollupPolicyConfig) -> dict[str, str]:
    return {
        "bot_display_name": config.bot_display_name,
        "timezone": config.timezone,
    }


def _empty_detailed_status() -> ConversationRollupDetailedStatus:
    return ConversationRollupDetailedStatus(
        scope=None,
        semantic=None,
        overlay=None,
        effective_coverage=0,
        rewrite_pending=False,
        semantic_uncovered_event_count=0,
        semantic_uncovered_character_count=0,
        effective_prompt_tail_event_count=0,
        effective_prompt_tail_character_count=0,
        job=None,
    )


def _checkpoint_status_from_semantic(
    row: CanonicalConversationRollupModel | None,
) -> RollupCheckpointStatus | None:
    if row is None:
        return None
    return RollupCheckpointStatus(
        kind=RollupKind(row.summary_kind),
        revision=row.revision,
        covered_through_event_id=row.covered_through_event_id,
    )


def _checkpoint_status_from_overlay(
    row: CanonicalConversationRollupEmergencyOverlayModel,
) -> RollupCheckpointStatus:
    return RollupCheckpointStatus(
        kind=RollupKind.EMERGENCY,
        revision=row.revision,
        covered_through_event_id=row.covered_through_event_id,
    )


def _job_status_from_row(
    job: CanonicalConversationRollupJobModel | None,
) -> RollupJobMetadata | None:
    if job is None:
        return None
    return RollupJobMetadata(
        status=job.status,
        signal_revision=job.signal_revision,
        failure_count=job.failure_count,
        created_at=job.created_at,
        last_error_category=job.last_error_category,
    )


def _job_state_dict(
    job: CanonicalConversationRollupJobModel | None,
) -> dict[str, object] | None:
    if job is None:
        return None
    return {
        "status": job.status,
        "signal_revision": job.signal_revision,
        "failure_count": job.failure_count,
        "created_at": job.created_at,
        "last_error_category": job.last_error_category,
    }


async def _load_prompt_tail_events(
    session: AsyncSession,
    *,
    scope: ConversationScope,
    coverage: int,
    last_event_id: int,
    conversation_id: str,
    before_event_id: int | None = None,
) -> tuple[EventRecord, ...]:
    del scope
    query = select(ChatEventModel).where(
        ChatEventModel.canonical_conversation_id == conversation_id,
        ChatEventModel.id > coverage,
        ChatEventModel.id <= last_event_id,
        keeper_event_clause(),
    )
    if before_event_id is not None:
        query = query.where(ChatEventModel.id < before_event_id)
    rows = tuple((await session.scalars(query.order_by(ChatEventModel.id.asc()))).all())
    return tuple(_event_record(row) for row in rows)


async def _compose_detailed_status(
    session: AsyncSession,
    *,
    state: ConversationScopeState,
    semantic_row: CanonicalConversationRollupModel | None,
    overlay_row: CanonicalConversationRollupEmergencyOverlayModel | None,
    job_row: CanonicalConversationRollupJobModel | None,
    conversation_id: str,
    config: RollupPolicyConfig,
) -> ConversationRollupDetailedStatus:
    semantic = _checkpoint_status_from_semantic(semantic_row)
    semantic_revision = semantic_row.revision if semantic_row is not None else 0
    overlay = None
    if _valid_same_generation_overlay(
        overlay_row,
        generation=state.generation,
        starts_after=state.starts_after_event_id,
        last_event_id=state.last_event_id,
        semantic_revision=semantic_revision,
    ):
        assert overlay_row is not None
        overlay = _checkpoint_status_from_overlay(overlay_row)
    effective_coverage = session_effective_coverage(
        generation=state.generation,
        starts_after=state.starts_after_event_id,
        last_event_id=state.last_event_id,
        overlay=overlay_row,
        semantic=semantic_row,
    )
    tail_events = 0
    tail_characters = 0
    if state.starts_after_event_id <= effective_coverage <= state.last_event_id:
        events = await _load_prompt_tail_events(
            session,
            scope=state.scope,
            coverage=effective_coverage,
            last_event_id=state.last_event_id,
            conversation_id=conversation_id,
        )
        tail_events = prompt_visible_event_count(events, **_prompt_kwargs(config))
        tail_characters = prompt_accounting_characters(events, **_prompt_kwargs(config))
    return ConversationRollupDetailedStatus(
        scope=state,
        semantic=semantic,
        overlay=overlay,
        effective_coverage=effective_coverage,
        rewrite_pending=overlay is not None,
        semantic_uncovered_event_count=state.uncovered_event_count,
        semantic_uncovered_character_count=state.uncovered_character_count,
        effective_prompt_tail_event_count=tail_events,
        effective_prompt_tail_character_count=tail_characters,
        job=_job_status_from_row(job_row),
    )


def protected_tail_start(events: tuple[EventRecord, ...], config: RollupPolicyConfig) -> int:
    """Return the start of the raw suffix that holds the last N visible messages.

    N is ``raw_tail_events``. Interleaved external rows ride with that
    contiguous suffix and do not occupy a visible-message slot.
    ``raw_tail_characters`` is not part of this boundary.
    """

    if not events:
        return 0
    remaining_visible = config.raw_tail_events
    start = len(events)
    for index in range(len(events) - 1, -1, -1):
        if is_prompt_visible_message(events[index], **_prompt_kwargs(config)):
            start = index
            remaining_visible -= 1
            if remaining_visible == 0:
                break
    return start


def eligible_prefix(
    events: tuple[EventRecord, ...], config: RollupPolicyConfig
) -> tuple[EventRecord, ...]:
    return events[: protected_tail_start(events, config)]


def exceeds_high_watermark(events: tuple[EventRecord, ...], config: RollupPolicyConfig) -> bool:
    characters = prompt_accounting_characters(events, **_prompt_kwargs(config))
    return len(events) >= config.trigger_events or characters >= config.trigger_characters


def exceeds_low_watermark(events: tuple[EventRecord, ...], config: RollupPolicyConfig) -> bool:
    if not events:
        return False
    characters = prompt_accounting_characters(events, **_prompt_kwargs(config))
    return len(events) > config.stop_events or characters > config.stop_characters


def take_batch(
    events: tuple[EventRecord, ...], config: RollupPolicyConfig
) -> tuple[EventRecord, ...]:
    selected: list[EventRecord] = []
    timezone = config.timezone
    cap = config.batch_max_characters
    for event in events:
        trial = (*selected, event)
        unbounded = len(serialize_compaction_source_events(trial, timezone=timezone))
        if selected and (len(selected) >= config.batch_max_events or unbounded > cap):
            break
        selected.append(event)
        if len(selected) >= config.batch_max_events or unbounded > cap:
            break
    return tuple(selected)


class ConversationScopeRepository:
    """Read scopes and enforce generation fences."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def get(self, scope: ConversationScope) -> ConversationScopeState | None:
        async with self._database.sessions() as session:
            alias = await session.scalar(
                select(ConversationLegacyAliasModel).where(
                    ConversationLegacyAliasModel.scope_key == scope.key
                )
            )
            if alias is None:
                return None
            conversation = await session.get(CanonicalConversationModel, alias.conversation_id)
            if conversation is None:
                return None
            return await hydrate_scope_state_from_canonical(session, scope, conversation)

    async def generation_matches(
        self, scope_id: int, generation: int, *, scope_key: str | None = None
    ) -> bool:
        del scope_id
        if not scope_key:
            return False
        async with self._database.sessions() as session:
            alias = await session.scalar(
                select(ConversationLegacyAliasModel).where(
                    ConversationLegacyAliasModel.scope_key == scope_key
                )
            )
            if alias is None:
                return False
            conversation = await session.get(CanonicalConversationModel, alias.conversation_id)
            return conversation is not None and int(conversation.generation) == generation


class ConversationRollupRepository:
    """Single-checkpoint reads, leases, candidate construction, and CAS commits."""

    def __init__(
        self,
        database: Database,
        config: RollupPolicyConfig,
        metrics: ConversationRollupMetrics | None = None,
    ) -> None:
        self._database = database
        self.config = config
        self.metrics = metrics or ConversationRollupMetrics()

    async def health_snapshot(self) -> dict[str, object]:
        """Return content-free, low-cardinality process health for all scopes."""

        now = _utcnow()
        async with self._database.sessions() as session:
            return await self._health_snapshot_canonical(session, now)

    async def status(
        self, scope: ConversationScope
    ) -> tuple[
        ConversationScopeState | None, ConversationRollupState | None, dict[str, object] | None
    ]:
        async with self._database.sessions() as session:
            return await self._status_canonical(session, scope)

    async def detailed_status(self, scope: ConversationScope) -> ConversationRollupDetailedStatus:
        """Metadata-only status. Does not load or return summary text."""

        async with self._database.sessions() as session:
            return await self._detailed_status_canonical(session, scope)

    async def load_prompt_snapshot(
        self,
        scope: ConversationScope,
        *,
        before_event_id: int | None = None,
    ) -> ConversationPromptSnapshot:
        """Load scope, checkpoint, and the exact continuous raw suffix in one transaction."""

        async with self._database.sessions() as session, session.begin():
            return await self._load_prompt_snapshot_canonical(
                session, scope, before_event_id=before_event_id
            )

    async def claim_next_job(
        self, *, lease_owner: str, lease_seconds: int
    ) -> RollupJobClaim | None:
        now = _utcnow()
        lease_until = now + timedelta(seconds=lease_seconds)
        token = uuid.uuid4().hex
        async with self._database.sessions() as session, session.begin():
            return await self._claim_next_canonical_job(
                session, lease_owner=lease_owner, lease_until=lease_until, token=token, now=now
            )

    async def claim_scope_for_foreground(
        self,
        scope: ConversationScope,
        *,
        lease_owner: str,
        lease_seconds: int,
    ) -> RollupJobClaim | None:
        """Preempt background ownership so foreground can restore a bounded prompt."""

        now = _utcnow()
        lease_until = now + timedelta(seconds=lease_seconds)
        token = uuid.uuid4().hex
        async with self._database.immediate_session() as session:
            return await self._claim_canonical_scope_for_foreground(
                session,
                scope,
                lease_owner=lease_owner,
                lease_until=lease_until,
                token=token,
                now=now,
            )

    async def heartbeat(self, claim: RollupJobClaim, *, lease_seconds: int) -> RollupJobClaim:
        now = _utcnow()
        renewed = now + timedelta(seconds=lease_seconds)
        if not claim.conversation_id:
            raise RollupLeaseLostError("canonical rollup claim has no conversation")
        async with self._database.sessions() as session, session.begin():
            result = await session.execute(
                update(CanonicalConversationRollupJobModel)
                .where(*self._canonical_lease_conditions(claim, now=now))
                .values(lease_until=renewed, updated_at=now)
            )
            if not cast(CursorResult[object], result).rowcount:
                raise RollupLeaseLostError("rollup heartbeat lost its lease")
        return RollupJobClaim(
            scope_id=claim.scope_id,
            generation=claim.generation,
            claimed_signal_revision=claim.claimed_signal_revision,
            failure_count=claim.failure_count,
            lease_owner=claim.lease_owner,
            lease_token=claim.lease_token,
            lease_until=renewed,
            conversation_id=claim.conversation_id,
        )

    async def candidate_for_claim(
        self, claim: RollupJobClaim, *, emergency: bool = False
    ) -> RollupCandidate | None:
        now = _utcnow()
        if not claim.conversation_id:
            raise RollupLeaseLostError("canonical rollup claim has no conversation")
        async with self._database.sessions() as session, session.begin():
            return await self._candidate_for_canonical(session, claim, now=now, emergency=emergency)

    async def finish_without_candidate(self, claim: RollupJobClaim) -> bool:
        """Delete only an unchanged job; otherwise restore it to pending."""

        now = _utcnow()
        if not claim.conversation_id:
            raise RollupLeaseLostError("canonical rollup claim has no conversation")
        async with self._database.sessions() as session, session.begin():
            result = await session.execute(
                delete(CanonicalConversationRollupJobModel).where(
                    *self._canonical_lease_conditions(claim, now=now),
                    CanonicalConversationRollupJobModel.signal_revision
                    == claim.claimed_signal_revision,
                )
            )
            if cast(CursorResult[object], result).rowcount:
                return True
            result = await session.execute(
                update(CanonicalConversationRollupJobModel)
                .where(*self._canonical_lease_conditions(claim, now=now))
                .values(
                    status="pending",
                    lease_owner=None,
                    lease_token=None,
                    lease_until=None,
                    next_attempt_at=now,
                    updated_at=now,
                )
            )
            if not cast(CursorResult[object], result).rowcount:
                raise RollupLeaseLostError("rollup idle completion lost its lease")
        return False

    async def commit_candidate(
        self,
        claim: RollupJobClaim,
        candidate: RollupCandidate,
        *,
        summary_text: str,
        summary_kind: RollupKind,
        retain_lease: bool = False,
    ) -> RollupCommitResult:
        if summary_kind is RollupKind.EMERGENCY:
            raise ValueError("emergency summaries cannot write the semantic rollup checkpoint")
        normalized = summary_text.strip()
        if not normalized or len(normalized) > self.config.summary_max_characters:
            raise ValueError("summary violates configured output bounds")
        now = _utcnow()
        if not claim.conversation_id:
            raise RollupLeaseLostError("canonical rollup claim has no conversation")
        async with self._database.sessions() as session, session.begin():
            return await self._commit_canonical_candidate(
                session,
                claim,
                candidate,
                summary_text=normalized,
                summary_kind=summary_kind,
                retain_lease=retain_lease,
                now=now,
            )

    async def commit_emergency_overlay(
        self,
        claim: RollupJobClaim,
        candidate: RollupCandidate,
        summary_text: str,
        *,
        error_category: str | None = None,
        disposition: EmergencyOverlayDisposition = EmergencyOverlayDisposition.FOREGROUND,
        source_emergency: bool = True,
        retry_max_seconds: int = _DEFAULT_RETRY_MAX_SECONDS,
    ) -> RollupCommitResult:
        """Upsert a prompt overlay only. Never mutates semantic rollup coverage."""

        normalized = summary_text.strip()
        if not normalized or len(normalized) > self.config.summary_max_characters:
            raise ValueError("summary violates configured output bounds")
        now = _utcnow()
        if not claim.conversation_id:
            raise RollupLeaseLostError("canonical rollup claim has no conversation")
        async with self._database.sessions() as session, session.begin():
            return await self._commit_canonical_emergency_overlay(
                session,
                claim,
                candidate,
                summary_text=normalized,
                now=now,
                error_category=error_category,
                disposition=disposition,
                source_emergency=source_emergency,
                retry_max_seconds=retry_max_seconds,
            )

    async def retry_infrastructure(
        self,
        claim: RollupJobClaim,
        *,
        error_category: str,
        retry_max_seconds: int,
    ) -> None:
        now = _utcnow()
        if not claim.conversation_id:
            raise RollupLeaseLostError("canonical rollup claim has no conversation")
        async with self._database.sessions() as session, session.begin():
            job = await session.scalar(
                select(CanonicalConversationRollupJobModel).where(
                    *self._canonical_lease_conditions(claim, now=now)
                )
            )
            if job is None:
                raise RollupLeaseLostError("rollup retry lost its lease")
            failure_count = job.failure_count + 1
            delay = _overlay_backoff_seconds(failure_count, retry_max_seconds)
            job.status = "pending"
            job.failure_count = failure_count
            job.lease_owner = None
            job.lease_token = None
            job.lease_until = None
            job.next_attempt_at = now + timedelta(seconds=delay)
            job.last_error_category = error_category[:64]
            job.updated_at = now

    async def release_owner(self, lease_owner: str) -> int:
        now = _utcnow()
        async with self._database.sessions() as session, session.begin():
            result = await session.execute(
                update(CanonicalConversationRollupJobModel)
                .where(
                    CanonicalConversationRollupJobModel.status == "processing",
                    CanonicalConversationRollupJobModel.lease_owner == lease_owner,
                )
                .values(
                    status="pending",
                    lease_owner=None,
                    lease_token=None,
                    lease_until=None,
                    next_attempt_at=now,
                    updated_at=now,
                )
            )
            return int(cast(CursorResult[object], result).rowcount or 0)

    async def _health_snapshot_canonical(
        self, session: AsyncSession, now: datetime
    ) -> dict[str, object]:
        scope_count, max_events, max_characters = (
            await session.execute(
                select(
                    func.count(CanonicalConversationModel.id),
                    func.max(CanonicalConversationModel.uncovered_event_count),
                    func.max(CanonicalConversationModel.uncovered_character_count),
                )
            )
        ).one()
        expired_processing = int(
            await session.scalar(
                select(func.count(CanonicalConversationRollupJobModel.conversation_id)).where(
                    CanonicalConversationRollupJobModel.status == "processing",
                    CanonicalConversationRollupJobModel.lease_until <= now,
                )
            )
            or 0
        )
        oldest_pending = await session.scalar(
            select(func.min(CanonicalConversationRollupJobModel.created_at)).where(
                CanonicalConversationRollupJobModel.status == "pending"
            )
        )
        recent_error = await session.scalar(
            select(CanonicalConversationRollupJobModel.last_error_category)
            .where(CanonicalConversationRollupJobModel.last_error_category.is_not(None))
            .order_by(CanonicalConversationRollupJobModel.updated_at.desc())
            .limit(1)
        )
        last_extractive = await session.scalar(
            select(func.max(CanonicalConversationRollupModel.updated_at)).where(
                CanonicalConversationRollupModel.summary_kind == RollupKind.EXTRACTIVE.value
            )
        )
        return {
            "scope_count": int(scope_count or 0),
            "expired_processing_leases": expired_processing,
            "oldest_pending_job_age_seconds": _age_seconds(oldest_pending, now),
            "max_lag_events": int(max_events or 0),
            "max_lag_characters": int(max_characters or 0),
            "recent_infrastructure_error_category": recent_error,
            "last_extractive_at": _as_utc_iso(last_extractive),
        }

    async def _claim_canonical_scope_for_foreground(
        self,
        session: AsyncSession,
        scope: ConversationScope,
        *,
        lease_owner: str,
        lease_until: datetime,
        token: str,
        now: datetime,
    ) -> RollupJobClaim | None:
        conversation = await self._conversation_for_scope(session, scope)
        if conversation is None:
            return None
        job = await session.get(CanonicalConversationRollupJobModel, conversation.id)
        if job is None:
            job = CanonicalConversationRollupJobModel(
                conversation_id=conversation.id,
                generation=conversation.generation,
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
            session.add(job)
            await session.flush()
        elif job.generation != conversation.generation:
            job.generation = conversation.generation
            job.signal_revision += 1
            job.failure_count = 0
            job.last_error_category = None
        job.status = "processing"
        job.lease_owner = lease_owner
        job.lease_token = token
        job.lease_until = lease_until
        job.next_attempt_at = now
        job.updated_at = now
        return RollupJobClaim(
            scope_id=synthetic_scope_id(conversation.id),
            generation=conversation.generation,
            claimed_signal_revision=job.signal_revision,
            failure_count=job.failure_count,
            lease_owner=lease_owner,
            lease_token=token,
            lease_until=lease_until,
            conversation_id=conversation.id,
        )

    async def _status_canonical(
        self, session: AsyncSession, scope: ConversationScope
    ) -> tuple[
        ConversationScopeState | None, ConversationRollupState | None, dict[str, object] | None
    ]:
        conversation = await self._conversation_for_scope(session, scope)
        if conversation is None:
            return None, None, None
        rollup_row = await session.get(CanonicalConversationRollupModel, conversation.id)
        overlay_row = await session.get(
            CanonicalConversationRollupEmergencyOverlayModel, conversation.id
        )
        job = await session.get(CanonicalConversationRollupJobModel, conversation.id)
        semantic_revision = rollup_row.revision if rollup_row is not None else 0
        effective = (
            self._canonical_rollup_state(rollup_row)
            if rollup_row is not None and rollup_row.generation == conversation.generation
            else None
        )
        if _valid_same_generation_overlay(
            overlay_row,
            generation=conversation.generation,
            starts_after=conversation.starts_after_event_id,
            last_event_id=conversation.last_event_id,
            semantic_revision=semantic_revision,
        ):
            assert overlay_row is not None
            effective = _canonical_overlay_state(overlay_row, synthetic_scope_id(conversation.id))
        return (
            await hydrate_scope_state_from_canonical(session, scope, conversation),
            effective,
            _job_state_dict(job),
        )

    async def _detailed_status_canonical(
        self, session: AsyncSession, scope: ConversationScope
    ) -> ConversationRollupDetailedStatus:
        conversation = await self._conversation_for_scope(session, scope)
        if conversation is None:
            return _empty_detailed_status()
        rollup_row = await session.get(CanonicalConversationRollupModel, conversation.id)
        overlay_row = await session.get(
            CanonicalConversationRollupEmergencyOverlayModel, conversation.id
        )
        job = await session.get(CanonicalConversationRollupJobModel, conversation.id)
        return await _compose_detailed_status(
            session,
            state=await hydrate_scope_state_from_canonical(session, scope, conversation),
            semantic_row=rollup_row,
            overlay_row=overlay_row,
            job_row=job,
            conversation_id=conversation.id,
            config=self.config,
        )

    async def _load_prompt_snapshot_canonical(
        self,
        session: AsyncSession,
        scope: ConversationScope,
        *,
        before_event_id: int | None,
    ) -> ConversationPromptSnapshot:
        conversation = await self._conversation_for_scope(session, scope)
        if conversation is None:
            raise ConversationCoverageError("conversation scope does not exist")
        state = await hydrate_scope_state_from_canonical(session, scope, conversation)
        rollup_row = await session.get(CanonicalConversationRollupModel, conversation.id)
        if rollup_row is not None and rollup_row.generation != conversation.generation:
            raise ConversationCoverageError("rollup generation mismatch")
        overlay_row = await session.get(
            CanonicalConversationRollupEmergencyOverlayModel, conversation.id
        )
        semantic_revision = rollup_row.revision if rollup_row is not None else 0
        overlay_state = None
        if _valid_same_generation_overlay(
            overlay_row,
            generation=conversation.generation,
            starts_after=conversation.starts_after_event_id,
            last_event_id=conversation.last_event_id,
            semantic_revision=semantic_revision,
        ):
            assert overlay_row is not None
            overlay_state = _canonical_overlay_state(overlay_row, state.id)
        coverage = session_effective_coverage(
            generation=conversation.generation,
            starts_after=conversation.starts_after_event_id,
            last_event_id=conversation.last_event_id,
            overlay=overlay_row,
            semantic=rollup_row,
        )
        events = await _load_prompt_tail_events(
            session,
            scope=scope,
            coverage=coverage,
            last_event_id=conversation.last_event_id,
            conversation_id=conversation.id,
            before_event_id=before_event_id,
        )
        tail_end = events[-1].id if events else coverage
        effective = (
            overlay_state
            if overlay_state is not None
            else (self._canonical_rollup_state(rollup_row) if rollup_row is not None else None)
        )
        return ConversationPromptSnapshot(
            scope=state,
            rollup=effective,
            raw_events=events,
            effective_coverage=coverage,
            raw_tail_end_event_id=tail_end,
            overlay=overlay_state,
            rewrite_pending=overlay_state is not None,
        )

    async def _claim_next_canonical_job(
        self,
        session: AsyncSession,
        *,
        lease_owner: str,
        lease_until: datetime,
        token: str,
        now: datetime,
    ) -> RollupJobClaim | None:
        candidate_id = await session.scalar(
            select(CanonicalConversationRollupJobModel.conversation_id)
            .where(
                or_(
                    and_(
                        CanonicalConversationRollupJobModel.status == "pending",
                        CanonicalConversationRollupJobModel.next_attempt_at <= now,
                    ),
                    and_(
                        CanonicalConversationRollupJobModel.status == "processing",
                        CanonicalConversationRollupJobModel.lease_until <= now,
                    ),
                )
            )
            .order_by(CanonicalConversationRollupJobModel.next_attempt_at.asc())
            .limit(1)
        )
        if candidate_id is None:
            return None
        row = await session.scalar(
            update(CanonicalConversationRollupJobModel)
            .where(
                CanonicalConversationRollupJobModel.conversation_id == candidate_id,
                or_(
                    and_(
                        CanonicalConversationRollupJobModel.status == "pending",
                        CanonicalConversationRollupJobModel.next_attempt_at <= now,
                    ),
                    and_(
                        CanonicalConversationRollupJobModel.status == "processing",
                        CanonicalConversationRollupJobModel.lease_until <= now,
                    ),
                ),
            )
            .values(
                status="processing",
                lease_owner=lease_owner,
                lease_token=token,
                lease_until=lease_until,
                updated_at=now,
            )
            .returning(CanonicalConversationRollupJobModel)
        )
        if row is None:
            return None
        return RollupJobClaim(
            scope_id=synthetic_scope_id(str(row.conversation_id)),
            generation=row.generation,
            claimed_signal_revision=row.signal_revision,
            failure_count=row.failure_count,
            lease_owner=lease_owner,
            lease_token=token,
            lease_until=lease_until,
            conversation_id=str(row.conversation_id),
        )

    async def _candidate_for_canonical(
        self,
        session: AsyncSession,
        claim: RollupJobClaim,
        *,
        now: datetime,
        emergency: bool = False,
    ) -> RollupCandidate | None:
        job = await session.get(CanonicalConversationRollupJobModel, claim.conversation_id)
        if job is None or not self._canonical_lease_matches(job, claim, now=now):
            raise RollupLeaseLostError("rollup candidate read lost its lease")
        conversation = await session.get(CanonicalConversationModel, claim.conversation_id)
        if conversation is None or conversation.generation != claim.generation:
            return None
        rollup = await session.get(CanonicalConversationRollupModel, claim.conversation_id)
        if rollup is not None and rollup.generation != claim.generation:
            raise ConversationCoverageError("rollup generation mismatch")
        overlay = await session.get(
            CanonicalConversationRollupEmergencyOverlayModel, claim.conversation_id
        )
        coverage, revision, previous = _source_checkpoint(
            emergency=emergency,
            overlay=overlay,
            semantic=rollup,
            generation=claim.generation,
            starts_after=conversation.starts_after_event_id,
            last_event_id=conversation.last_event_id,
        )
        rows = tuple(
            (
                await session.scalars(
                    select(ChatEventModel)
                    .where(
                        ChatEventModel.canonical_conversation_id == conversation.id,
                        ChatEventModel.id > coverage,
                        ChatEventModel.id <= conversation.last_event_id,
                        keeper_event_clause(),
                    )
                    .order_by(ChatEventModel.id.asc())
                )
            ).all()
        )
        all_events = tuple(_event_record(row) for row in rows)
        batch = take_batch(eligible_prefix(all_events, self.config), self.config)
        if not batch:
            return None
        characters = source_accounting_characters(
            batch,
            timezone=self.config.timezone,
            max_characters=self.config.batch_max_characters,
        )
        fingerprint = source_fingerprint(
            scope_id=claim.scope_id,
            generation=claim.generation,
            source_coverage=coverage,
            source_rollup_revision=revision,
            previous_summary=previous,
            events=batch,
        )
        return RollupCandidate(
            scope_id=claim.scope_id,
            generation=claim.generation,
            source_coverage=coverage,
            source_rollup_revision=revision,
            previous_summary=previous,
            events=batch,
            event_count=len(batch),
            projection_characters=characters,
            fingerprint=fingerprint,
            conversation_id=claim.conversation_id,
        )

    async def _commit_canonical_candidate(
        self,
        session: AsyncSession,
        claim: RollupJobClaim,
        candidate: RollupCandidate,
        *,
        summary_text: str,
        summary_kind: RollupKind,
        retain_lease: bool,
        now: datetime,
    ) -> RollupCommitResult:
        if summary_kind is RollupKind.EMERGENCY:
            raise ValueError("emergency summaries cannot write the semantic rollup checkpoint")
        job = await session.get(CanonicalConversationRollupJobModel, claim.conversation_id)
        if job is None or not self._canonical_lease_matches(job, claim, now=now):
            raise RollupLeaseLostError("rollup commit lost its lease")
        conversation = await session.get(CanonicalConversationModel, claim.conversation_id)
        if conversation is None or conversation.generation != candidate.generation:
            raise RollupSourceChangedError("scope generation changed")
        current_rollup = await session.get(CanonicalConversationRollupModel, claim.conversation_id)
        current_overlay = await session.get(
            CanonicalConversationRollupEmergencyOverlayModel, claim.conversation_id
        )
        coverage = (
            current_rollup.covered_through_event_id
            if current_rollup is not None
            else conversation.starts_after_event_id
        )
        revision = current_rollup.revision if current_rollup is not None else 0
        previous = current_rollup.summary_text if current_rollup is not None else ""
        if coverage != candidate.source_coverage or revision != candidate.source_rollup_revision:
            raise RollupSourceChangedError("rollup checkpoint changed")
        rows = tuple(
            (
                await session.scalars(
                    select(ChatEventModel)
                    .where(
                        ChatEventModel.canonical_conversation_id == conversation.id,
                        ChatEventModel.id > coverage,
                        ChatEventModel.id <= candidate.events[-1].id,
                        keeper_event_clause(),
                    )
                    .order_by(ChatEventModel.id.asc())
                )
            ).all()
        )
        events = tuple(_event_record(row) for row in rows)
        fingerprint = source_fingerprint(
            scope_id=candidate.scope_id,
            generation=candidate.generation,
            source_coverage=coverage,
            source_rollup_revision=revision,
            previous_summary=previous,
            events=events,
        )
        if (
            tuple(event.id for event in events) != tuple(event.id for event in candidate.events)
            or fingerprint != candidate.fingerprint
        ):
            raise RollupSourceChangedError("rollup source projection changed")
        covered_through = candidate.events[-1].id
        remaining_rows = tuple(
            (
                await session.scalars(
                    select(ChatEventModel)
                    .where(
                        ChatEventModel.canonical_conversation_id == conversation.id,
                        ChatEventModel.id > covered_through,
                        ChatEventModel.id <= conversation.last_event_id,
                        keeper_event_clause(),
                    )
                    .order_by(ChatEventModel.id.asc())
                )
            ).all()
        )
        remaining = tuple(_event_record(row) for row in remaining_rows)
        statement = insert(CanonicalConversationRollupModel).values(
            conversation_id=claim.conversation_id,
            generation=candidate.generation,
            covered_through_event_id=covered_through,
            summary_text=summary_text,
            summary_kind=summary_kind.value,
            source_fingerprint=candidate.fingerprint,
            revision=revision + 1,
            created_at=current_rollup.created_at if current_rollup is not None else now,
            updated_at=now,
        )
        await session.execute(
            statement.on_conflict_do_update(
                index_elements=[CanonicalConversationRollupModel.conversation_id],
                set_={
                    "generation": candidate.generation,
                    "covered_through_event_id": covered_through,
                    "summary_text": summary_text,
                    "summary_kind": summary_kind.value,
                    "source_fingerprint": candidate.fingerprint,
                    "revision": revision + 1,
                    "updated_at": now,
                },
            )
        )
        if current_rollup is not None:
            current_rollup.generation = candidate.generation
            current_rollup.covered_through_event_id = covered_through
            current_rollup.summary_text = summary_text
            current_rollup.summary_kind = summary_kind.value
            current_rollup.source_fingerprint = candidate.fingerprint
            current_rollup.revision = revision + 1
            current_rollup.updated_at = now
        conversation.uncovered_event_count = len(remaining)
        conversation.uncovered_character_count = durable_uncovered_characters(
            remaining, **_prompt_kwargs(self.config)
        )
        conversation.covered_through_event_id = covered_through
        conversation.updated_at = now
        overlay_ahead = await _reconcile_overlay_with_semantic(
            session,
            current_overlay,
            generation=candidate.generation,
            covered_through=covered_through,
            next_semantic_revision=revision + 1,
        )
        job.failure_count = 0
        job.last_error_category = None
        continue_work = (
            exceeds_low_watermark(eligible_prefix(remaining, self.config), self.config)
            or overlay_ahead
        )
        signal_changed = job.signal_revision != claim.claimed_signal_revision
        retained = bool(retain_lease and (continue_work or signal_changed))
        if retained:
            job.updated_at = now
        elif continue_work or signal_changed:
            job.status = "pending"
            job.lease_owner = None
            job.lease_token = None
            job.lease_until = None
            job.next_attempt_at = now
            job.updated_at = now
        else:
            await session.delete(job)
        await session.flush()
        stored = await session.get(CanonicalConversationRollupModel, claim.conversation_id)
        if stored is None:
            raise ConversationCoverageError("rollup commit did not persist")
        return RollupCommitResult(
            rollup=self._canonical_rollup_state(stored),
            claim_retained=retained,
        )

    async def _commit_canonical_emergency_overlay(
        self,
        session: AsyncSession,
        claim: RollupJobClaim,
        candidate: RollupCandidate,
        *,
        summary_text: str,
        now: datetime,
        error_category: str | None = None,
        disposition: EmergencyOverlayDisposition = EmergencyOverlayDisposition.FOREGROUND,
        source_emergency: bool = True,
        retry_max_seconds: int = _DEFAULT_RETRY_MAX_SECONDS,
    ) -> RollupCommitResult:
        job = await session.get(CanonicalConversationRollupJobModel, claim.conversation_id)
        if job is None or not self._canonical_lease_matches(job, claim, now=now):
            raise RollupLeaseLostError("rollup overlay commit lost its lease")
        conversation = await session.get(CanonicalConversationModel, claim.conversation_id)
        if conversation is None or conversation.generation != candidate.generation:
            raise RollupSourceChangedError("scope generation changed")
        current_rollup = await session.get(CanonicalConversationRollupModel, claim.conversation_id)
        current_overlay = await session.get(
            CanonicalConversationRollupEmergencyOverlayModel, claim.conversation_id
        )
        coverage, revision, previous = _source_checkpoint(
            emergency=source_emergency,
            overlay=current_overlay,
            semantic=current_rollup,
            generation=candidate.generation,
            starts_after=conversation.starts_after_event_id,
            last_event_id=conversation.last_event_id,
        )
        if coverage != candidate.source_coverage or revision != candidate.source_rollup_revision:
            raise RollupSourceChangedError("rollup checkpoint changed")
        rows = tuple(
            (
                await session.scalars(
                    select(ChatEventModel)
                    .where(
                        ChatEventModel.canonical_conversation_id == conversation.id,
                        ChatEventModel.id > coverage,
                        ChatEventModel.id <= candidate.events[-1].id,
                        keeper_event_clause(),
                    )
                    .order_by(ChatEventModel.id.asc())
                )
            ).all()
        )
        events = tuple(_event_record(row) for row in rows)
        fingerprint = source_fingerprint(
            scope_id=candidate.scope_id,
            generation=candidate.generation,
            source_coverage=coverage,
            source_rollup_revision=revision,
            previous_summary=previous,
            events=events,
        )
        if (
            tuple(event.id for event in events) != tuple(event.id for event in candidate.events)
            or fingerprint != candidate.fingerprint
        ):
            raise RollupSourceChangedError("rollup source projection changed")
        covered_through = candidate.events[-1].id
        semantic_revision = current_rollup.revision if current_rollup is not None else 0
        valid_overlay = _valid_same_generation_overlay(
            current_overlay,
            generation=candidate.generation,
            starts_after=conversation.starts_after_event_id,
            last_event_id=conversation.last_event_id,
            semantic_revision=semantic_revision,
        )
        if _overlay_should_replace(
            current_overlay, valid=valid_overlay, covered_through=covered_through
        ):
            overlay_revision = (
                current_overlay.revision if valid_overlay and current_overlay is not None else 0
            )
            created_at = (
                current_overlay.created_at if valid_overlay and current_overlay is not None else now
            )
            statement = insert(CanonicalConversationRollupEmergencyOverlayModel).values(
                conversation_id=claim.conversation_id,
                generation=candidate.generation,
                covered_through_event_id=covered_through,
                summary_text=summary_text,
                source_fingerprint=candidate.fingerprint,
                base_semantic_revision=semantic_revision,
                revision=overlay_revision + 1,
                created_at=created_at,
                updated_at=now,
            )
            await session.execute(
                statement.on_conflict_do_update(
                    index_elements=[
                        CanonicalConversationRollupEmergencyOverlayModel.conversation_id
                    ],
                    set_={
                        "generation": candidate.generation,
                        "covered_through_event_id": covered_through,
                        "summary_text": summary_text,
                        "source_fingerprint": candidate.fingerprint,
                        "base_semantic_revision": semantic_revision,
                        "revision": overlay_revision + 1,
                        "updated_at": now,
                    },
                )
            )
        _dispose_job_after_overlay(
            job,
            claim,
            now,
            disposition=disposition,
            error_category=error_category,
            retry_max_seconds=retry_max_seconds,
        )
        await session.flush()
        stored = await session.get(
            CanonicalConversationRollupEmergencyOverlayModel, claim.conversation_id
        )
        if stored is None:
            raise ConversationCoverageError("emergency overlay commit did not persist")
        return RollupCommitResult(
            rollup=_canonical_overlay_state(stored, claim.scope_id),
            claim_retained=False,
        )

    @staticmethod
    async def _conversation_for_scope(
        session: AsyncSession, scope: ConversationScope
    ) -> CanonicalConversationModel | None:
        alias = await session.scalar(
            select(ConversationLegacyAliasModel).where(
                ConversationLegacyAliasModel.scope_key == scope.key
            )
        )
        if alias is None:
            return None
        return await session.get(CanonicalConversationModel, alias.conversation_id)

    @staticmethod
    def _canonical_rollup_state(row: CanonicalConversationRollupModel) -> ConversationRollupState:
        return ConversationRollupState(
            scope_id=synthetic_scope_id(row.conversation_id),
            generation=row.generation,
            covered_through_event_id=row.covered_through_event_id,
            summary_text=row.summary_text,
            summary_kind=RollupKind(row.summary_kind),
            source_fingerprint=row.source_fingerprint,
            revision=row.revision,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    @staticmethod
    def _canonical_lease_conditions(
        claim: RollupJobClaim,
        *,
        now: datetime,
    ) -> tuple[ColumnElement[bool], ...]:
        return (
            CanonicalConversationRollupJobModel.conversation_id == claim.conversation_id,
            CanonicalConversationRollupJobModel.status == "processing",
            CanonicalConversationRollupJobModel.lease_owner == claim.lease_owner,
            CanonicalConversationRollupJobModel.lease_token == claim.lease_token,
            CanonicalConversationRollupJobModel.lease_until > now,
        )

    @staticmethod
    def _canonical_lease_matches(
        row: CanonicalConversationRollupJobModel, claim: RollupJobClaim, *, now: datetime
    ) -> bool:
        lease_until = row.lease_until
        if lease_until is not None and lease_until.tzinfo is None:
            lease_until = lease_until.replace(tzinfo=UTC)
        return (
            row.status == "processing"
            and row.lease_owner == claim.lease_owner
            and row.lease_token == claim.lease_token
            and lease_until is not None
            and lease_until > now
        )


async def recount_canonical_uncovered(
    session: AsyncSession,
    conversation: CanonicalConversationModel,
    config: RollupPolicyConfig | None = None,
) -> tuple[int, int]:
    """Repair canonical uncovered counters for live keeper events."""

    event_count, character_count = await calculate_canonical_uncovered(
        session,
        conversation,
        config,
    )
    conversation.uncovered_event_count = event_count
    conversation.uncovered_character_count = character_count
    return event_count, character_count


async def calculate_canonical_uncovered(
    session: AsyncSession,
    conversation: CanonicalConversationModel,
    config: RollupPolicyConfig | None = None,
) -> tuple[int, int]:
    """Calculate the durable keeper/message rulers without mutating the conversation."""

    rollup = await session.get(CanonicalConversationRollupModel, conversation.id)
    if rollup is not None and rollup.generation != conversation.generation:
        raise ConversationCoverageError("cannot recount across rollup generations")
    coverage = rollup.covered_through_event_id if rollup else conversation.starts_after_event_id
    rows = tuple(
        (
            await session.scalars(
                select(ChatEventModel)
                .where(
                    ChatEventModel.canonical_conversation_id == conversation.id,
                    ChatEventModel.id > coverage,
                    ChatEventModel.id <= conversation.last_event_id,
                    keeper_event_clause(),
                )
                .order_by(ChatEventModel.id.asc())
            )
        ).all()
    )
    events = tuple(_event_record(row) for row in rows)
    policy = config or RollupPolicyConfig()
    character_count = durable_uncovered_characters(
        events,
        **_prompt_kwargs(policy),
    )
    return len(events), character_count
