"""Unit and service tests for persistent affection and trust relationships."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import uuid4

from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.domain.messages import (
    ChatRequest,
    ChatResponse,
)
from qq_ai_bot.identity.canonical_repository import (
    IDENTITY_PLATFORM,
    ensure_person,
)
from qq_ai_bot.identity.db_models import IdentityBindingModel
from qq_ai_bot.llm.base import LLMProvider
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.repositories import (
    EventLedgerRepository,
)


async def append_user_event(
    database: Database,
    *,
    message_id: str,
    content: str = "正常聊天",
    user_id: str = "1001",
) -> int:
    row, _ = await EventLedgerRepository(database).append(
        bot_user_id="8000",
        platform_message_id=message_id,
        scope_type=ScopeType.PRIVATE,
        sender_user_id=user_id,
        direction="inbound",
        content=content,
        private_peer_user_id=user_id,
    )
    return row.id


class CapturingRelationshipProvider(LLMProvider):
    def __init__(self, job_id: int, *, confidence: float = 0.9) -> None:
        self.job_id = job_id
        self.confidence = confidence
        self.request: ChatRequest | None = None

    async def complete(self, request: ChatRequest) -> ChatResponse:
        self.request = request
        return ChatResponse(
            content=json.dumps(
                {
                    "evaluations": [
                        {
                            "job_id": self.job_id,
                            "affection_delta": 1,
                            "trust_delta": 1,
                            "reason_code": "respectful_interaction",
                            "confidence": self.confidence,
                        }
                    ]
                }
            ),
            latency_seconds=0,
        )


async def _add_canonical_person_with_aliases(
    database: Database,
    primary: str,
    *aliases: str,
) -> str:
    now = datetime(2026, 8, 26, tzinfo=UTC)
    async with database.sessions() as session, session.begin():
        person_id = await ensure_person(session, primary, display_name="primary", now=now)
        for alias in aliases:
            session.add(
                IdentityBindingModel(
                    id=str(uuid4()),
                    person_id=person_id,
                    platform=IDENTITY_PLATFORM,
                    external_account_id=alias,
                    display_name="alias",
                    status="active",
                    revision=1,
                    first_seen_at=now,
                    last_seen_at=now,
                    created_at=now,
                    updated_at=now,
                )
            )
    return person_id
