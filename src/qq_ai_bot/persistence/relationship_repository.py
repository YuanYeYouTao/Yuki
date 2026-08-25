"""Repositories for relationship state, history, and evaluation jobs."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, or_, select, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.domain.relationships import (
    RelationshipEvaluation,
    RelationshipSnapshot,
)
from qq_ai_bot.identity.canonical_projections import (
    canonical_relationship_owner_keys,
    load_canonical_relationship_events,
    owner_keys_for_person,
    require_person_binding,
    resolve_canonical_relationship,
    resolve_person_author_id_for_event,
)
from qq_ai_bot.identity.errors import IdentityDualWriteError
from qq_ai_bot.identity.runtime import identity_runtime_is_complete_v2
from qq_ai_bot.identity.shadows import fill_relationship_event_shadows
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.models import (
    ChatEventModel,
    PersonModel,
    PersonRelationshipModel,
    RelationshipEventModel,
    RelationshipJobModel,
)
from qq_ai_bot.persistence.repository_helpers import (
    _ensure_person,
    _ensure_relationship,
    _event_record,
    _relationship_event_record,
    _relationship_snapshot,
)
from qq_ai_bot.persistence.repository_records import (
    RelationshipEventRecord,
    RelationshipJobRecord,
)


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
            _relationship_snapshot(row, trust_cap_offset=self._trust_cap_offset),
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
    ) -> PersonRelationshipModel:
        if await identity_runtime_is_complete_v2(session):
            row = await resolve_canonical_relationship(
                session,
                user_id,
                create=True,
                initial_affection=(
                    self._initial_affection if initial_affection is None else initial_affection
                ),
                initial_trust=self._initial_trust if initial_trust is None else initial_trust,
                now=now,
            )
            if row is None:
                raise IdentityDualWriteError("unclassified")
            return row
        person = await session.get(PersonModel, user_id)
        if person is None:
            await _ensure_person(session, user_id, now=now)
        return await _ensure_relationship(
            session,
            user_id,
            initial_affection=(
                self._initial_affection if initial_affection is None else initial_affection
            ),
            initial_trust=(self._initial_trust if initial_trust is None else initial_trust),
            now=now,
        )

    async def get_or_create(
        self,
        user_id: str,
        *,
        initial_affection: int | None = None,
        initial_trust: int | None = None,
        session: AsyncSession | None = None,
    ) -> RelationshipSnapshot:
        if session is None:
            async with self._database.sessions() as probe:
                complete_v2 = await identity_runtime_is_complete_v2(probe)
            if complete_v2:
                async with self._database.immediate_session() as writer:
                    return await self.get_or_create(
                        user_id,
                        initial_affection=initial_affection,
                        initial_trust=initial_trust,
                        session=writer,
                    )
            async with self._database.sessions() as owned_session, owned_session.begin():
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
            if await identity_runtime_is_complete_v2(session):
                row = await resolve_canonical_relationship(
                    session,
                    user_id,
                    create=False,
                    initial_affection=self._initial_affection,
                    initial_trust=self._initial_trust,
                )
                if row is None:
                    return None
                return self._projected_snapshot(row, user_id)
            row = await session.get(PersonRelationshipModel, user_id)
            if row is None:
                return None
            return _relationship_snapshot(row, trust_cap_offset=self._trust_cap_offset)

    async def get_many(
        self,
        user_ids: tuple[str, ...],
    ) -> dict[str, RelationshipSnapshot]:
        """Load existing relationship rows without creating unrelated people."""

        unique_ids = tuple(dict.fromkeys(user_ids))
        if not unique_ids:
            return {}
        async with self._database.sessions() as session:
            if await identity_runtime_is_complete_v2(session):
                loaded: dict[str, RelationshipSnapshot] = {}
                for item in unique_ids:
                    row = await resolve_canonical_relationship(
                        session,
                        item,
                        create=False,
                        initial_affection=self._initial_affection,
                        initial_trust=self._initial_trust,
                    )
                    if row is not None:
                        loaded[item] = self._projected_snapshot(row, item)
                return loaded
            rows = (
                await session.scalars(
                    select(PersonRelationshipModel).where(
                        PersonRelationshipModel.user_id.in_(unique_ids)
                    )
                )
            ).all()
        return {
            row.user_id: _relationship_snapshot(
                row,
                trust_cap_offset=self._trust_cap_offset,
            )
            for row in rows
        }

    async def history(
        self,
        user_id: str,
        *,
        limit: int = 10,
    ) -> tuple[RelationshipEventRecord, ...]:
        async with self._database.sessions() as session:
            if await identity_runtime_is_complete_v2(session):
                rows = await load_canonical_relationship_events(session, user_id, limit=limit)
                return tuple(
                    replace(_relationship_event_record(row), user_id=user_id) for row in rows
                )
            rows = tuple(
                (
                    await session.scalars(
                        select(RelationshipEventModel)
                        .where(RelationshipEventModel.user_id == user_id)
                        .order_by(
                            RelationshipEventModel.created_at.desc(),
                            RelationshipEventModel.id.desc(),
                        )
                        .limit(max(1, min(limit, 100)))
                    )
                ).all()
            )
            return tuple(_relationship_event_record(row) for row in rows)

    async def apply_automatic(
        self,
        *,
        user_id: str,
        source_event_id: int,
        evaluation: RelationshipEvaluation,
        max_auto_delta: int | None = None,
        daily_positive_cap: int = 0,
        daily_negative_cap: int = 0,
    ) -> tuple[RelationshipSnapshot, bool]:
        """Apply one event once, with optional runtime daily caps (zero means unlimited)."""

        self._validate_automatic_evaluation(
            evaluation,
            maximum=max_auto_delta,
        )
        now = datetime.now(UTC)
        try:
            async with self._database.sessions() as session, session.begin():
                complete_v2 = await identity_runtime_is_complete_v2(session)
                existing = await session.scalar(
                    select(RelationshipEventModel.id).where(
                        RelationshipEventModel.change_type == "automatic",
                        RelationshipEventModel.source_event_id == source_event_id,
                    )
                )
                row = await self._ensure_row(session, user_id, now=now)
                if existing is not None:
                    return (self._projected_snapshot(row, user_id), False)
                source = await session.get(ChatEventModel, source_event_id)
                if source is None or source.direction != "inbound":
                    raise ValueError("relationship source event does not belong to the user")
                if complete_v2:
                    source_person = await resolve_person_author_id_for_event(session, source)
                    job_person = row.canonical_person_id
                    if job_person is None:
                        job_person, _ = await canonical_relationship_owner_keys(session, user_id)
                    if source_person is None or source_person != job_person:
                        raise ValueError("relationship source event does not belong to the user")
                elif source.sender_user_id != user_id:
                    raise ValueError("relationship source event does not belong to the user")

                effective_evaluation = evaluation
                if daily_positive_cap or daily_negative_cap:
                    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
                    if complete_v2:
                        person_id, owner_keys = await canonical_relationship_owner_keys(
                            session, user_id
                        )
                        daily_filter = or_(
                            RelationshipEventModel.canonical_person_id == person_id,
                            RelationshipEventModel.user_id.in_(owner_keys),
                        )
                    else:
                        daily_filter = RelationshipEventModel.user_id == user_id
                    daily_events = (
                        await session.scalars(
                            select(RelationshipEventModel).where(
                                daily_filter,
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
                    user_id=row.user_id if complete_v2 else user_id,
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
                )
                session.add(event)
                if complete_v2:
                    await fill_relationship_event_shadows(
                        session, event, person_id=row.canonical_person_id
                    )
                await session.flush()
                return (self._projected_snapshot(row, user_id), True)
        except IntegrityError:
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
            async with self._database.sessions() as probe:
                complete_v2 = await identity_runtime_is_complete_v2(probe)
            if complete_v2:
                async with self._database.immediate_session() as writer:
                    return await self._apply_manual(
                        user_id=user_id,
                        actor_user_id=actor_user_id,
                        reason_code=reason_code,
                        affection_score=affection_score,
                        affection_delta=affection_delta,
                        trust_score=trust_score,
                        session=writer,
                    )
            async with self._database.sessions() as owned_session, owned_session.begin():
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
        complete_v2 = await identity_runtime_is_complete_v2(session)
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
            user_id=row.user_id if complete_v2 else user_id,
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
        )
        session.add(event)
        if complete_v2:
            await fill_relationship_event_shadows(session, event, person_id=row.canonical_person_id)
        await session.flush()
        return self._projected_snapshot(row, user_id)


class RelationshipJobRepository:
    """Restart-safe relationship queue with bounded retries and five-event context."""

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
            values: dict[str, object] = {
                "trigger_event_id": trigger_event_id,
                "user_id": user_id,
                "conversation_key": conversation_key,
                "status": "pending",
                "attempts": 0,
                "next_attempt_at": now,
                "error_category": None,
                "created_at": now,
                "updated_at": now,
            }
            if await identity_runtime_is_complete_v2(session):
                trigger = await session.get(ChatEventModel, trigger_event_id)
                if trigger is None:
                    raise IdentityDualWriteError("unclassified")
                person_id = await resolve_person_author_id_for_event(session, trigger)
                if person_id is None:
                    return
                caller = await require_person_binding(session, user_id)
                if caller.person_id != person_id:
                    raise IdentityDualWriteError("canonical_owner_mismatch")
                values["canonical_person_id"] = person_id
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
        async with self._database.sessions() as session, session.begin():
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
                trigger = await session.get(ChatEventModel, row.trigger_event_id)
                if trigger is None:
                    await session.delete(row)
                    continue
                recent_query = select(ChatEventModel).where(
                    ChatEventModel.id <= trigger.id,
                )
                if await identity_runtime_is_complete_v2(session):
                    person_id = row.canonical_person_id
                    if person_id is None:
                        try:
                            person_id, owner_keys = await canonical_relationship_owner_keys(
                                session, row.user_id
                            )
                        except IdentityDualWriteError:
                            await session.delete(row)
                            continue
                    else:
                        owner_keys = await owner_keys_for_person(session, person_id)
                    if trigger.scope_type == ScopeType.PRIVATE.value:
                        recent_query = recent_query.where(
                            or_(
                                ChatEventModel.private_peer_user_id.in_(owner_keys),
                                ChatEventModel.author_person_id == person_id,
                            )
                        )
                    else:
                        recent_query = recent_query.where(
                            ChatEventModel.group_id == trigger.group_id,
                            or_(
                                ChatEventModel.sender_user_id.in_(owner_keys),
                                ChatEventModel.author_person_id == person_id,
                            ),
                        )
                elif trigger.scope_type == ScopeType.PRIVATE.value:
                    recent_query = recent_query.where(
                        ChatEventModel.private_peer_user_id == row.user_id,
                    )
                else:
                    recent_query = recent_query.where(
                        ChatEventModel.group_id == trigger.group_id,
                        ChatEventModel.sender_user_id == row.user_id,
                    )
                recent_rows = list(
                    (
                        await session.scalars(
                            recent_query.order_by(ChatEventModel.id.desc()).limit(5)
                        )
                    ).all()
                )
                recent_rows.reverse()
                row.status = "processing"
                row.updated_at = now
                result.append(
                    RelationshipJobRecord(
                        job_id=row.id,
                        attempts=row.attempts,
                        user_id=row.user_id,
                        conversation_key=row.conversation_key,
                        trigger_event=_event_record(trigger),
                        recent_events=tuple(_event_record(event) for event in recent_rows),
                    )
                )
            return tuple(result)

    async def complete(self, job_ids: tuple[int, ...]) -> None:
        if not job_ids:
            return
        async with self._database.sessions() as session, session.begin():
            await session.execute(
                update(RelationshipJobModel)
                .where(RelationshipJobModel.id.in_(job_ids))
                .values(
                    status="completed",
                    updated_at=datetime.now(UTC),
                    error_category=None,
                )
            )

    async def fail(self, job_id: int, error_category: str) -> None:
        now = datetime.now(UTC)
        async with self._database.sessions() as session, session.begin():
            row = await session.get(RelationshipJobModel, job_id)
            if row is None:
                return
            row.attempts += 1
            row.status = "failed" if row.attempts >= self._max_attempts else "pending"
            row.next_attempt_at = now + timedelta(seconds=30 * row.attempts)
            row.updated_at = now
            row.error_category = error_category[:64]
