"""Canonical person observations and inbound message fixtures."""

from __future__ import annotations

from datetime import UTC, datetime

from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.domain.messages import InboundMessage, SenderIdentity
from qq_ai_bot.identity.canonical_repository import ensure_person, ensure_space
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.people_repository import PeopleRepository


async def observe_members(
    database: Database,
    user_ids: tuple[str, ...],
    *,
    group_id: str = "2001",
) -> PeopleRepository:
    """Seed real canonical owners, then exercise their ordinary observation path."""
    async with database.immediate_session() as session:
        await ensure_space(session, group_id, now=datetime(2026, 8, 24, tzinfo=UTC))
        for user_id in user_ids:
            await ensure_person(session, user_id, now=datetime(2026, 8, 24, tzinfo=UTC))
    people = PeopleRepository(database)
    for user_id in user_ids:
        await people.observe(
            user_id=user_id,
            nickname=f"name-{user_id}",
            group_id=group_id,
            group_card=f"card-{user_id}",
        )
    return people


def inbound(
    text: str,
    *,
    message_id: str,
    nickname: str = "",
    group_card: str = "",
    group_id: str | None = None,
    mentions_bot: bool = False,
    user_id: str = "1001",
    mentioned_user_ids: tuple[str, ...] = (),
) -> InboundMessage:
    return InboundMessage(
        message_id=message_id,
        event_type="message:test",
        scope_type=ScopeType.GROUP if group_id is not None else ScopeType.PRIVATE,
        sender=SenderIdentity(
            user_id=user_id,
            nickname=nickname,
            group_card=group_card,
        ),
        text=text,
        bot_user_id="9999",
        group_id=group_id,
        mentions_bot=mentions_bot,
        mentioned_user_ids=mentioned_user_ids,
    )
