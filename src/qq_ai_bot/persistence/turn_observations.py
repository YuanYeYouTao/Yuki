"""Persistence for content-free runtime turn observations (3.6.0-R1)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel
from qq_ai_bot.identity.db_models import CanonicalPersonModel, CanonicalSpaceModel
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.models import RuntimeTurnObservationModel
from qq_ai_bot.runtime.observability import RuntimeTurnObservation


async def _require_canonical(
    session: AsyncSession,
    model: type[Any],
    value: str | None,
    *,
    label: str,
) -> str | None:
    if value is None:
        return None
    canonical_id = str(value).strip()
    if not canonical_id:
        return None
    row = await session.get(model, canonical_id)
    if row is None:
        raise ValueError(f"v2 observation {label} does not exist")
    return canonical_id


class RuntimeTurnObservationRepository:
    """Store one bounded row per admitted turn; never any content."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def record_turn(self, observation: RuntimeTurnObservation) -> None:
        async with self._database.sessions() as session, session.begin():
            conversation_id = await _require_canonical(
                session,
                CanonicalConversationModel,
                observation.canonical_conversation_id,
                label="conversation",
            )
            person_id = await _require_canonical(
                session,
                CanonicalPersonModel,
                observation.canonical_person_id,
                label="person",
            )
            space_id = await _require_canonical(
                session,
                CanonicalSpaceModel,
                observation.canonical_space_id,
                label="space",
            )
            if person_id is not None and space_id is not None:
                raise ValueError("observation cannot have both person and space")
            if observation.scope_type == "private" and space_id is not None:
                raise ValueError("private observation cannot have space")
            if observation.scope_type == "group" and person_id is not None:
                raise ValueError("group observation cannot have person")
            row = RuntimeTurnObservationModel(
                runtime_turn_id=observation.runtime_turn_id[:64],
                origin=observation.origin.value[:32],
                scope_type=observation.scope_type[:16],
                conversation_key_hash=observation.conversation_key_hash,
                admission_outcome=observation.admission_outcome,
                handled=observation.handled,
                sent_messages=max(0, observation.sent_messages),
                error_category=observation.error_category,
                total_latency_ms=max(0, observation.total_latency_ms),
                created_at=observation.created_at,
                expires_at=observation.expires_at,
                canonical_conversation_id=conversation_id,
                canonical_person_id=person_id,
                canonical_space_id=space_id,
            )
            session.add(row)

    async def cleanup_expired(self, *, now: datetime | None = None, limit: int = 500) -> int:
        """Delete one bounded batch of expired rows; call repeatedly to drain."""

        cutoff = now or datetime.now(UTC)
        async with self._database.sessions() as session, session.begin():
            ids = tuple(
                await session.scalars(
                    select(RuntimeTurnObservationModel.id)
                    .where(RuntimeTurnObservationModel.expires_at <= cutoff)
                    .order_by(
                        RuntimeTurnObservationModel.expires_at,
                        RuntimeTurnObservationModel.id,
                    )
                    .limit(max(1, limit))
                )
            )
            if not ids:
                return 0
            await session.execute(
                delete(RuntimeTurnObservationModel).where(RuntimeTurnObservationModel.id.in_(ids))
            )
            return len(ids)
