"""Identity, lifecycle, queue, and context contracts for Memory V2."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from sqlalchemy.exc import IntegrityError
from tests.conftest import make_settings
from tests.support.model_executor import InjectedModelExecutor

from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.domain.messages import ChatRequest, ChatResponse
from qq_ai_bot.llm.base import LLMProvider
from qq_ai_bot.memory.enums import (
    MemoryEvidenceRelation,
    MemoryKind,
    MemoryScopeType,
    MemorySourceType,
)
from qq_ai_bot.memory.extraction import (
    ExtractedMemoryClaim,
)
from qq_ai_bot.memory.models import MemoryEvidenceCreate, MemoryFactCreate
from qq_ai_bot.memory.repository import MemoryFactRepository, MemoryJobRepository
from qq_ai_bot.memory.service import MemoryFactService
from qq_ai_bot.memory.validation import MemoryClaimValidator
from qq_ai_bot.memory.worker import MemoryWorker
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.repositories import EventLedgerRepository
from qq_ai_bot.persistence.repository_records import EventRecord
from qq_ai_bot.services.concurrency import ConcurrencyManager


def _event(
    *,
    event_id: int = 1,
    sender_user_id: str = "1001",
    scope_type: ScopeType = ScopeType.PRIVATE,
    group_id: str | None = None,
) -> EventRecord:
    return EventRecord(
        id=event_id,
        bot_user_id="8000",
        platform_message_id=f"event-{event_id}",
        scope_type=scope_type,
        sender_user_id=sender_user_id,
        direction="inbound",
        content="我准备考研",
        visual_summary="",
        segments=(),
        occurred_at=datetime.now(UTC),
        group_id=group_id,
        private_peer_user_id=sender_user_id if scope_type is ScopeType.PRIVATE else None,
    )


def _claim(**overrides: object) -> ExtractedMemoryClaim:
    values: dict[str, object] = {
        "subject_ref": "speaker",
        "scope_type": "person",
        "kind": "fact",
        "memory_key": "education:plan",
        "category": "education",
        "content": "准备考研",
        "evidence_quote": "我准备考研",
        "importance": 4,
        "confidence": 0.9,
        "source_type": "automatic",
    }
    values.update(overrides)
    return ExtractedMemoryClaim.model_validate(values)


def test_validator_owns_event_and_speaker_identity() -> None:
    event = _event(event_id=42, sender_user_id="1001")
    validated = MemoryClaimValidator().validate(_claim(), event)
    assert validated is not None
    fact, evidence = validated
    assert fact.subject_user_id == "1001"
    assert fact.group_id is None
    assert evidence.event_id == 42
    assert evidence.source_speaker_user_id == "1001"


def test_validator_rejects_unknown_subject_and_private_group_claims() -> None:
    event = _event()
    validator = MemoryClaimValidator()
    assert validator.validate(_claim(subject_ref="other_person"), event) is None
    assert (
        validator.validate(
            _claim(subject_ref="group", scope_type="group"),
            event,
        )
        is None
    )


def test_validator_rejects_non_event_evidence_but_trusts_model_paraphrase() -> None:
    event = _event()
    validator = MemoryClaimValidator()
    assert validator.validate(_claim(evidence_quote="上下文里有人准备考研"), event) is None
    assert (
        validator.validate(
            _claim(content="准备出国", evidence_quote="我准备考研"),
            event,
        )
        is not None
    )


async def _append_event(
    ledger: EventLedgerRepository,
    *,
    message_id: str,
    user_id: str = "1001",
    content: str = "我准备考研",
    group_id: str | None = None,
    direction: str = "inbound",
    sender_is_bot: bool = False,
) -> EventRecord:
    row, _ = await ledger.append(
        bot_user_id="8000",
        platform_message_id=message_id,
        scope_type=ScopeType.GROUP if group_id else ScopeType.PRIVATE,
        sender_user_id=user_id,
        direction=direction,
        content=content,
        group_id=group_id,
        private_peer_user_id=None if group_id else user_id,
        sender_is_bot=sender_is_bot,
    )
    return row


def _fact(
    *,
    content: str,
    memory_key: str = "education:plan",
    source_type: MemorySourceType = MemorySourceType.AUTOMATIC,
    user_id: str | None = "1001",
    group_id: str | None = None,
    scope_type: MemoryScopeType = MemoryScopeType.PERSON,
    kind: MemoryKind = MemoryKind.FACT,
) -> MemoryFactCreate:
    return MemoryFactCreate(
        scope_type=scope_type,
        subject_user_id=user_id,
        group_id=group_id,
        kind=kind,
        memory_key=memory_key,
        category="test",
        content=content,
        importance=4,
        confidence=0.9,
        source_type=source_type,
    )


@pytest.mark.asyncio
async def test_fact_and_evidence_write_rolls_back_as_one_transaction(database: Database) -> None:
    service = MemoryFactService(MemoryFactRepository(database))
    with pytest.raises(IntegrityError):
        await service.remember(
            _fact(content="事务测试"),
            evidence=MemoryEvidenceCreate(
                event_id=999_999,
                source_speaker_user_id="1001",
                relation=MemoryEvidenceRelation.SELF_STATEMENT,
                excerpt="不存在的事件",
            ),
        )
    assert not await service.list_person("1001")


@pytest.mark.asyncio
async def test_jobs_accept_only_real_inbound_non_bot_events(database: Database) -> None:
    ledger = EventLedgerRepository(database)
    inbound = await _append_event(ledger, message_id="job-inbound")
    outbound = await _append_event(
        ledger,
        message_id="job-outbound",
        user_id="8000",
        group_id="2001",
        direction="outbound",
        sender_is_bot=True,
    )
    bot_inbound = await _append_event(
        ledger,
        message_id="job-bot",
        user_id="7000",
        group_id="2001",
        sender_is_bot=True,
    )
    blank = await _append_event(ledger, message_id="job-blank", content="   ")
    jobs = MemoryJobRepository(database)

    assert await jobs.enqueue(inbound.id, "private:1001")
    assert not await jobs.enqueue(inbound.id, "private:1001")
    assert not await jobs.enqueue(outbound.id, "private:1001")
    assert not await jobs.enqueue(bot_inbound.id, "private:7000")
    assert not await jobs.enqueue(blank.id, "private:1001")


class _CancelledProvider(LLMProvider):
    async def complete(self, request: ChatRequest) -> ChatResponse:
        raise asyncio.CancelledError


@pytest.mark.asyncio
async def test_worker_propagates_cancellation(database: Database) -> None:
    ledger = EventLedgerRepository(database)
    event = await _append_event(ledger, message_id="worker-cancel")
    jobs = MemoryJobRepository(database)
    assert await jobs.enqueue(event.id, "private:1001")
    worker = MemoryWorker(
        settings=make_settings(database.url, memory_batch_max_wait_seconds=0),
        jobs=jobs,
        facts=MemoryFactService(MemoryFactRepository(database)),
        ledger=ledger,
        model_executor=InjectedModelExecutor(_CancelledProvider()),
        concurrency=ConcurrencyManager(1),
    )
    with pytest.raises(asyncio.CancelledError):
        await worker.process_once()
