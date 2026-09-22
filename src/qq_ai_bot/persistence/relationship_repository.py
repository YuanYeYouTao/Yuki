"""Repositories for relationship state, history, and evaluation jobs."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, case, func, or_, select, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel
from qq_ai_bot.domain.relationships import (
    RelationshipEvaluation,
    RelationshipSnapshot,
)
from qq_ai_bot.identity.canonical_repository import (
    bindings_for_person,
    representative_external_account_id,
    require_person_binding,
    resolve_person_author_id_for_event,
)
from qq_ai_bot.identity.errors import CanonicalIdentityError
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.job_claims import PreparedJobClaim, commit_job_claims
from qq_ai_bot.persistence.models import (
    ChatEventModel,
    PersonRelationshipModel,
    RelationshipEventModel,
    RelationshipJobModel,
)
from qq_ai_bot.persistence.repository_helpers import (
    _event_record,
    _relationship_event_record,
    _relationship_snapshot,
    keeper_event_clause,
)
from qq_ai_bot.persistence.repository_records import (
    RelationshipEventRecord,
    RelationshipJobRecord,
)


class RelationshipClaimLost(RuntimeError):
    """A result no longer owns a live claim and its original evidence generation."""


def _claim_generation_current(job: RelationshipJobRecord) -> ColumnElement[bool]:
    return (
        select(CanonicalConversationModel.id)
        .where(
            CanonicalConversationModel.id == job.trigger_event.canonical_conversation_id,
            CanonicalConversationModel.generation == job.conversation_generation,
        )
        .exists()
    )


async def _complete_claim(session: AsyncSession, job: RelationshipJobRecord) -> bool:
    identity = await session.scalar(
        update(RelationshipJobModel)
        .where(
            RelationshipJobModel.id == job.job_id,
            RelationshipJobModel.status == "processing",
            RelationshipJobModel.updated_at == job.claimed_at,
            _claim_generation_current(job),
        )
        .values(status="completed", updated_at=datetime.now(UTC), error_category=None)
        .returning(RelationshipJobModel.id)
    )
    return identity is not None


class RelationshipRepository:
    """Persist bounded per-person affection and trust with a complete audit trail."""

    def __init__(
        self,
        database: Database,
        *,
        initial_affection: int = 50,
        initial_trust: int = 50,
        trust_cap_offset: int = 10,
        max_affection_auto_delta: int = 2,
        max_trust_auto_delta: int = 2,
    ) -> None:
        self._database = database
        self._initial_affection = initial_affection
        self._initial_trust = initial_trust
        self._trust_cap_offset = trust_cap_offset
        self._max_affection_auto_delta = max_affection_auto_delta
        self._max_trust_auto_delta = max_trust_auto_delta

    def _projected_snapshot(
        self, row: PersonRelationshipModel, user_id: str
    ) -> RelationshipSnapshot:
        return replace(
            _relationship_snapshot(
                row,
                user_id=user_id,
                trust_cap_offset=self._trust_cap_offset,
            ),
            user_id=user_id,
        )

    async def _ensure_row(
        self,
        session: AsyncSession,
        user_id: str,
        *,
        now: datetime,
        initial_affection: int | None = None,
        initial_trust: int | None = None,
        flush: bool = True,
    ) -> PersonRelationshipModel:
        binding = await require_person_binding(session, user_id)
        row = await session.get(PersonRelationshipModel, binding.person_id)
        if row is None:
            row = PersonRelationshipModel(
                canonical_person_id=binding.person_id,
                affection_score=(
                    self._initial_affection if initial_affection is None else initial_affection
                ),
                trust_score=self._initial_trust if initial_trust is None else initial_trust,
                created_at=now,
                updated_at=now,
                last_automatic_change_at=None,
            )
            session.add(row)
            if flush:
                await session.flush()
        return row

    async def get_or_create(
        self,
        user_id: str,
        *,
        initial_affection: int | None = None,
        initial_trust: int | None = None,
        session: AsyncSession | None = None,
    ) -> RelationshipSnapshot:
        if session is None:
            async with self._database.immediate_session() as owned_session:
                return await self.get_or_create(
                    user_id,
                    initial_affection=initial_affection,
                    initial_trust=initial_trust,
                    session=owned_session,
                )
        now = datetime.now(UTC)
        row = await self._ensure_row(
            session,
            user_id,
            now=now,
            initial_affection=initial_affection,
            initial_trust=initial_trust,
        )
        await session.flush()
        return self._projected_snapshot(row, user_id)

    async def get(self, user_id: str) -> RelationshipSnapshot | None:
        async with self._database.sessions() as session:
            binding = await require_person_binding(session, user_id)
            row = await session.get(PersonRelationshipModel, binding.person_id)
            if row is None:
                return None
            return self._projected_snapshot(row, user_id)

    async def get_many(
        self,
        user_ids: tuple[str, ...],
    ) -> dict[str, RelationshipSnapshot]:
        """Load existing relationship rows without creating unrelated people."""

        unique_ids = tuple(dict.fromkeys(user_ids))
        if not unique_ids:
            return {}
        async with self._database.sessions() as session:
            loaded: dict[str, RelationshipSnapshot] = {}
            for item in unique_ids:
                binding = await require_person_binding(session, item)
                row = await session.get(PersonRelationshipModel, binding.person_id)
                if row is not None:
                    loaded[item] = self._projected_snapshot(row, item)
            return loaded

    async def history(
        self,
        user_id: str,
        *,
        limit: int = 10,
    ) -> tuple[RelationshipEventRecord, ...]:
        async with self._database.sessions() as session:
            binding = await require_person_binding(session, user_id)
            rows = (
                await session.scalars(
                    select(RelationshipEventModel)
                    .where(RelationshipEventModel.canonical_person_id == binding.person_id)
                    .order_by(
                        RelationshipEventModel.created_at.desc(),
                        RelationshipEventModel.id.desc(),
                    )
                    .limit(max(1, min(limit, 100)))
                )
            ).all()
            return tuple(_relationship_event_record(row, user_id=user_id) for row in rows)

    async def apply_automatic(
        self,
        *,
        user_id: str,
        source_event_id: int,
        evaluation: RelationshipEvaluation,
        max_auto_delta: int | None = None,
        daily_positive_cap: int = 0,
        daily_negative_cap: int = 0,
        claim: RelationshipJobRecord | None = None,
    ) -> tuple[RelationshipSnapshot, bool]:
        """Apply one event once, with optional runtime daily caps (zero means unlimited)."""

        self._validate_automatic_evaluation(
            evaluation,
            maximum=max_auto_delta,
        )
        if claim is not None and (
            claim.user_id != user_id or claim.trigger_event.id != source_event_id
        ):
            raise ValueError("relationship claim does not match the evaluated event")
        now = datetime.now(UTC)
        try:
            async with self._database.sessions(autoflush=False) as session, session.begin():
                existing = await session.scalar(
                    select(RelationshipEventModel.id).where(
                        RelationshipEventModel.change_type == "automatic",
                        RelationshipEventModel.source_event_id == source_event_id,
                    )
                )
                row = await self._ensure_row(session, user_id, now=now, flush=False)
                source = await session.get(ChatEventModel, source_event_id)
                if source is None or source.direction != "inbound":
                    raise ValueError("relationship source event does not belong to the user")
                source_person = await resolve_person_author_id_for_event(session, source)
                job_person = row.canonical_person_id
                if source_person is None or source_person != job_person:
                    raise ValueError("relationship source event does not belong to the user")
                if existing is not None:
                    if claim is not None and not await _complete_claim(session, claim):
                        raise RelationshipClaimLost("relationship job ownership changed")
                    return (self._projected_snapshot(row, user_id), False)

                effective_evaluation = evaluation
                if daily_positive_cap or daily_negative_cap:
                    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
                    daily_events = (
                        await session.scalars(
                            select(RelationshipEventModel).where(
                                RelationshipEventModel.canonical_person_id == job_person,
                                RelationshipEventModel.change_type == "automatic",
                                RelationshipEventModel.created_at >= day_start,
                            )
                        )
                    ).all()
                    affection_delta = self._apply_daily_cap(
                        evaluation.affection_delta,
                        positive_used=sum(max(0, item.affection_delta) for item in daily_events),
                        negative_used=sum(max(0, -item.affection_delta) for item in daily_events),
                        positive_cap=daily_positive_cap,
                        negative_cap=daily_negative_cap,
                    )
                    trust_delta = self._apply_daily_cap(
                        evaluation.trust_delta,
                        positive_used=sum(max(0, item.trust_delta) for item in daily_events),
                        negative_used=sum(max(0, -item.trust_delta) for item in daily_events),
                        positive_cap=daily_positive_cap,
                        negative_cap=daily_negative_cap,
                    )
                    effective_evaluation = RelationshipEvaluation(
                        affection_delta=affection_delta,
                        trust_delta=trust_delta,
                        reason_code=(
                            evaluation.reason_code if affection_delta or trust_delta else "neutral"
                        ),
                        confidence=evaluation.confidence,
                    )
                # This is the first write, after identity/evidence/cap reads.
                # Finish the claim and score change in one transaction, so a
                # stale worker cannot publish a score before losing completion.
                if claim is not None and not await _complete_claim(session, claim):
                    raise RelationshipClaimLost("relationship job ownership changed")
                if claim is not None and row not in session.new:
                    unchanged = await session.scalar(
                        update(PersonRelationshipModel)
                        .where(
                            PersonRelationshipModel.canonical_person_id == job_person,
                            PersonRelationshipModel.updated_at == row.updated_at,
                            PersonRelationshipModel.affection_score == row.affection_score,
                            PersonRelationshipModel.trust_score == row.trust_score,
                        )
                        .values(updated_at=row.updated_at)
                        .returning(PersonRelationshipModel.canonical_person_id)
                    )
                    if unchanged is None:
                        raise RuntimeError("relationship state changed before commit")
                affection_before = row.affection_score
                trust_before = row.trust_score
                row.affection_score = max(
                    0,
                    min(100, affection_before + effective_evaluation.affection_delta),
                )
                row.trust_score = max(
                    0,
                    min(100, trust_before + effective_evaluation.trust_delta),
                )
                affection_delta = row.affection_score - affection_before
                trust_delta = row.trust_score - trust_before
                row.updated_at = now
                if affection_delta or trust_delta:
                    row.last_automatic_change_at = now
                event = RelationshipEventModel(
                    source_event_id=source_event_id,
                    actor_user_id=None,
                    change_type="automatic",
                    affection_before=affection_before,
                    affection_delta=affection_delta,
                    affection_after=row.affection_score,
                    trust_before=trust_before,
                    trust_delta=trust_delta,
                    trust_after=row.trust_score,
                    reason_code=effective_evaluation.reason_code[:64],
                    confidence=effective_evaluation.confidence,
                    created_at=now,
                    canonical_person_id=job_person,
                )
                session.add(event)
                await session.flush()
                return (self._projected_snapshot(row, user_id), True)
        except IntegrityError:
            if claim is not None:
                # The whole claim/score transaction rolled back. The worker
                # must retry or yield by its claim, not report it completed.
                raise
            snapshot = await self.get_or_create(user_id)
            return snapshot, False

    def _validate_automatic_evaluation(
        self,
        evaluation: RelationshipEvaluation,
        *,
        maximum: int | None = None,
    ) -> None:
        affection_maximum = maximum or self._max_affection_auto_delta
        trust_maximum = maximum or self._max_trust_auto_delta
        for value, maximum, name in (
            (
                evaluation.affection_delta,
                affection_maximum,
                "affection_delta",
            ),
            (
                evaluation.trust_delta,
                trust_maximum,
                "trust_delta",
            ),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or abs(value) > maximum:
                raise ValueError(f"{name} exceeds the configured automatic range")
        if not 0 <= evaluation.confidence <= 1:
            raise ValueError("confidence must be between zero and one")

    @staticmethod
    def _apply_daily_cap(
        delta: int,
        *,
        positive_used: int,
        negative_used: int,
        positive_cap: int,
        negative_cap: int,
    ) -> int:
        if delta > 0 and positive_cap > 0:
            return min(delta, max(0, positive_cap - positive_used))
        if delta < 0 and negative_cap > 0:
            return max(delta, -max(0, negative_cap - negative_used))
        return delta

    async def set_affection(
        self,
        *,
        user_id: str,
        actor_user_id: str,
        score: int,
        session: AsyncSession | None = None,
    ) -> RelationshipSnapshot:
        if not 0 <= score <= 100:
            raise ValueError("affection score must be between 0 and 100")
        return await self._apply_manual(
            user_id=user_id,
            actor_user_id=actor_user_id,
            affection_score=score,
            reason_code="manual_set_affection",
            session=session,
        )

    async def adjust_affection(
        self,
        *,
        user_id: str,
        actor_user_id: str,
        delta: int,
        session: AsyncSession | None = None,
    ) -> RelationshipSnapshot:
        if not -20 <= delta <= 20:
            raise ValueError("affection adjustment must be between -20 and 20")
        return await self._apply_manual(
            user_id=user_id,
            actor_user_id=actor_user_id,
            affection_delta=delta,
            reason_code="manual_adjust_affection",
            session=session,
        )

    async def set_trust(
        self,
        *,
        user_id: str,
        actor_user_id: str,
        score: int,
        session: AsyncSession | None = None,
    ) -> RelationshipSnapshot:
        if not 0 <= score <= 100:
            raise ValueError("trust score must be between 0 and 100")
        return await self._apply_manual(
            user_id=user_id,
            actor_user_id=actor_user_id,
            trust_score=score,
            reason_code="manual_set_trust",
            session=session,
        )

    async def _apply_manual(
        self,
        *,
        user_id: str,
        actor_user_id: str,
        reason_code: str,
        affection_score: int | None = None,
        affection_delta: int = 0,
        trust_score: int | None = None,
        session: AsyncSession | None = None,
    ) -> RelationshipSnapshot:
        if session is None:
            async with self._database.immediate_session() as owned_session:
                return await self._apply_manual(
                    user_id=user_id,
                    actor_user_id=actor_user_id,
                    reason_code=reason_code,
                    affection_score=affection_score,
                    affection_delta=affection_delta,
                    trust_score=trust_score,
                    session=owned_session,
                )
        now = datetime.now(UTC)
        row = await self._ensure_row(session, user_id, now=now)
        affection_before = row.affection_score
        trust_before = row.trust_score
        row.affection_score = (
            affection_score
            if affection_score is not None
            else max(0, min(100, affection_before + affection_delta))
        )
        row.trust_score = trust_score if trust_score is not None else trust_before
        actual_affection_delta = row.affection_score - affection_before
        actual_trust_delta = row.trust_score - trust_before
        row.updated_at = now
        event = RelationshipEventModel(
            source_event_id=None,
            actor_user_id=actor_user_id,
            change_type="manual",
            affection_before=affection_before,
            affection_delta=actual_affection_delta,
            affection_after=row.affection_score,
            trust_before=trust_before,
            trust_delta=actual_trust_delta,
            trust_after=row.trust_score,
            reason_code=reason_code,
            confidence=None,
            created_at=now,
            canonical_person_id=row.canonical_person_id,
        )
        session.add(event)
        await session.flush()
        return self._projected_snapshot(row, user_id)


class RelationshipJobRepository:
    """Restart-safe relationship queue with bounded retries and five-event context."""

    _BINDING_RETRY_DELAY = timedelta(minutes=1)

    def __init__(self, database: Database, *, max_attempts: int = 3) -> None:
        self._database = database
        self._max_attempts = max_attempts

    async def enqueue(
        self,
        *,
        trigger_event_id: int,
        user_id: str,
        conversation_key: str,
    ) -> None:
        now = datetime.now(UTC)
        async with self._database.sessions() as session, session.begin():
            trigger = await session.get(ChatEventModel, trigger_event_id)
            if trigger is None:
                raise CanonicalIdentityError("unclassified")
            person_id = await resolve_person_author_id_for_event(session, trigger)
            if person_id is None:
                return
            caller = await require_person_binding(session, user_id)
            if caller.person_id != person_id:
                raise CanonicalIdentityError("canonical_owner_mismatch")
            values: dict[str, object] = {
                "trigger_event_id": trigger_event_id,
                "conversation_key": conversation_key,
                "status": "pending",
                "attempts": 0,
                "next_attempt_at": now,
                "error_category": None,
                "created_at": now,
                "updated_at": now,
                "canonical_person_id": person_id,
            }
            statement = insert(RelationshipJobModel).values(**values)
            await session.execute(
                statement.on_conflict_do_nothing(
                    index_elements=[RelationshipJobModel.trigger_event_id]
                )
            )

    async def pending_count(self) -> int:
        async with self._database.sessions() as session:
            value = await session.scalar(
                select(func.count())
                .select_from(RelationshipJobModel)
                .where(
                    RelationshipJobModel.status == "pending",
                    RelationshipJobModel.next_attempt_at <= datetime.now(UTC),
                )
            )
            return int(value or 0)

    async def claim(self, *, limit: int = 10) -> tuple[RelationshipJobRecord, ...]:
        now = datetime.now(UTC)
        stale_processing = now - timedelta(minutes=5)
        prepared: list[PreparedJobClaim] = []
        async with self._database.sessions() as session:
            rows = (
                await session.scalars(
                    select(RelationshipJobModel)
                    .where(
                        or_(
                            RelationshipJobModel.status == "pending",
                            (
                                (RelationshipJobModel.status == "processing")
                                & (RelationshipJobModel.updated_at <= stale_processing)
                            ),
                        ),
                        RelationshipJobModel.next_attempt_at <= now,
                    )
                    .order_by(RelationshipJobModel.id)
                    .limit(max(1, min(limit, 100)))
                )
            ).all()
            result: list[RelationshipJobRecord] = []
            for row in rows:
                prior = (row.id, row.status, row.updated_at)
                # Read the evidence and its privacy generation in the same SQL
                # snapshot. A prior job may have cached this event before forget.
                source = (
                    await session.execute(
                        select(ChatEventModel, CanonicalConversationModel.generation)
                        .outerjoin(
                            CanonicalConversationModel,
                            CanonicalConversationModel.id
                            == ChatEventModel.canonical_conversation_id,
                        )
                        .where(ChatEventModel.id == row.trigger_event_id)
                        .execution_options(populate_existing=True)
                    )
                ).one_or_none()
                if source is None:
                    prepared.append(PreparedJobClaim(*prior, None))
                    continue
                trigger, generation = source
                if generation is None or trigger.canonical_conversation_id is None:
                    prepared.append(
                        PreparedJobClaim(
                            *prior,
                            {
                                "status": "failed",
                                "error_category": "missing_canonical_conversation",
                                "updated_at": now,
                            },
                        )
                    )
                    continue
                recent_query = select(ChatEventModel).where(
                    ChatEventModel.id <= trigger.id,
                    ChatEventModel.canonical_conversation_id == trigger.canonical_conversation_id,
                    ChatEventModel.author_person_id == row.canonical_person_id,
                    ChatEventModel.direction == "inbound",
                    ChatEventModel.event_kind == "message",
                    keeper_event_clause(),
                )
                person_id = row.canonical_person_id
                bindings = await bindings_for_person(session, person_id)
                try:
                    projected_user_id = representative_external_account_id(bindings)
                except CanonicalIdentityError:
                    # A job is owned by the canonical Person, not by whichever
                    # external Binding happens to be active when it is claimed.
                    # Provider/account switches therefore make delivery
                    # temporarily unavailable rather than invalidating work.
                    # Do not consume the bounded evaluator retry budget here:
                    # postpone the claim so a later active Binding can project
                    # the Person back to the transport-facing identifier.
                    prepared.append(
                        PreparedJobClaim(
                            *prior,
                            {
                                "status": "pending",
                                "next_attempt_at": now + self._BINDING_RETRY_DELAY,
                                "updated_at": now,
                                "error_category": "binding_unavailable",
                            },
                        )
                    )
                    continue
                recent_rows = list(
                    (
                        await session.scalars(
                            recent_query.order_by(ChatEventModel.id.desc())
                            .limit(5)
                            .execution_options(populate_existing=True)
                        )
                    ).all()
                )
                recent_rows.reverse()
                prepared.append(
                    PreparedJobClaim(
                        *prior,
                        {"status": "processing", "updated_at": now},
                        conversation_snapshot=(trigger.canonical_conversation_id, generation),
                    )
                )
                result.append(
                    RelationshipJobRecord(
                        job_id=row.id,
                        claimed_at=now,
                        conversation_generation=generation,
                        attempts=row.attempts,
                        user_id=projected_user_id,
                        conversation_key=row.conversation_key,
                        trigger_event=_event_record(trigger),
                        recent_events=tuple(_event_record(event) for event in recent_rows),
                    )
                )
        accepted = await commit_job_claims(self._database, RelationshipJobModel, prepared)
        return tuple(job for job in result if job.job_id in accepted)

    async def assert_current(self, jobs: tuple[RelationshipJobRecord, ...]) -> None:
        """Read-only dispatch fence; no lock or session survives the provider call."""
        if not jobs:
            return
        async with self._database.sessions() as session:
            current = set(
                await session.scalars(
                    select(RelationshipJobModel.id).where(
                        or_(
                            *(
                                and_(
                                    RelationshipJobModel.id == job.job_id,
                                    RelationshipJobModel.status == "processing",
                                    RelationshipJobModel.updated_at == job.claimed_at,
                                    _claim_generation_current(job),
                                )
                                for job in jobs
                            )
                        )
                    )
                )
            )
        if current != {job.job_id for job in jobs}:
            raise RelationshipClaimLost("relationship claim or evidence generation changed")

    async def complete(self, jobs: tuple[RelationshipJobRecord, ...]) -> None:
        if not jobs:
            return
        async with self._database.sessions() as session, session.begin():
            for job in jobs:
                await _complete_claim(session, job)

    async def defer(self, jobs: tuple[RelationshipJobRecord, ...]) -> None:
        """Yield live claims; terminate stale evidence without charging a failure."""
        if not jobs:
            return
        now = datetime.now(UTC)
        async with self._database.sessions() as session, session.begin():
            for job in jobs:
                await session.execute(
                    update(RelationshipJobModel)
                    .where(
                        RelationshipJobModel.id == job.job_id,
                        RelationshipJobModel.status == "processing",
                        RelationshipJobModel.updated_at == job.claimed_at,
                    )
                    .values(
                        status=case((_claim_generation_current(job), "pending"), else_="failed"),
                        next_attempt_at=now + timedelta(seconds=30),
                        updated_at=now,
                        error_category=case(
                            (_claim_generation_current(job), RelationshipJobModel.error_category),
                            else_="conversation_generation_changed",
                        ),
                    )
                )

    async def fail(self, job: RelationshipJobRecord, error_category: str) -> None:
        now = datetime.now(UTC)
        attempts = job.attempts + 1
        async with self._database.sessions() as session, session.begin():
            await session.execute(
                update(RelationshipJobModel)
                .where(
                    RelationshipJobModel.id == job.job_id,
                    RelationshipJobModel.status == "processing",
                    RelationshipJobModel.updated_at == job.claimed_at,
                )
                .values(
                    attempts=case(
                        (_claim_generation_current(job), attempts),
                        else_=RelationshipJobModel.attempts,
                    ),
                    status=case(
                        (
                            _claim_generation_current(job),
                            "failed" if attempts >= self._max_attempts else "pending",
                        ),
                        else_="failed",
                    ),
                    next_attempt_at=now + timedelta(seconds=30 * attempts),
                    updated_at=now,
                    error_category=case(
                        (_claim_generation_current(job), error_category[:64]),
                        else_="conversation_generation_changed",
                    ),
                )
            )
