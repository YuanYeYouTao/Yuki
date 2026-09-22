"""Commit prepared job transitions without holding a writer while loading evidence."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import delete, select, update

from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.models import ChatEventModel


@dataclass(frozen=True)
class PreparedJobClaim:
    job_id: int
    status: str
    updated_at: datetime
    values: dict[str, Any] | None
    live_event_id: int | None = None
    conversation_snapshot: tuple[str, int] | None = None


async def commit_job_claims(
    database: Database, model: Any, claims: list[PreparedJobClaim]
) -> set[int]:
    """Only claim unchanged rows; concurrent readers cannot both own a job."""
    accepted: set[int] = set()
    if not claims:
        return accepted
    async with database.sessions() as session, session.begin():
        for claim in claims:
            statement = (
                delete(model) if claim.values is None else update(model).values(**claim.values)
            )
            if claim.live_event_id is not None:
                statement = statement.where(
                    select(ChatEventModel.id)
                    .join(
                        CanonicalConversationModel,
                        CanonicalConversationModel.id == ChatEventModel.canonical_conversation_id,
                    )
                    .where(
                        ChatEventModel.id == claim.live_event_id,
                        ChatEventModel.canonical_event_id.is_not(None),
                        ChatEventModel.id > CanonicalConversationModel.starts_after_event_id,
                        ChatEventModel.id
                        > CanonicalConversationModel.last_generation_change_event_id,
                    )
                    .exists()
                )
            if claim.conversation_snapshot is not None:
                conversation_id, generation = claim.conversation_snapshot
                statement = statement.where(
                    select(CanonicalConversationModel.id)
                    .where(
                        CanonicalConversationModel.id == conversation_id,
                        CanonicalConversationModel.generation == generation,
                    )
                    .exists()
                )
            identity = await session.scalar(
                statement.where(
                    model.id == claim.job_id,
                    model.status == claim.status,
                    model.updated_at == claim.updated_at,
                ).returning(model.id)
            )
            if identity is not None:
                accepted.add(int(identity))
    return accepted
