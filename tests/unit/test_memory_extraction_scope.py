"""Ordinary extraction creates human/space facts, never a SELF judgment queue."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from tests.conftest import make_settings
from tests.support.model_executor import InjectedModelExecutor
from tests.unit.test_memory_v2 import _append_event, _claim

from yuki.llm.fake import FakeLLMProvider
from yuki.memory.enums import MemoryRebuildJobOutcome
from yuki.memory.event_extractor import MemoryEventExtractor
from yuki.memory.repository import MemoryFactRepository, MemoryJobRepository
from yuki.memory.service import MemoryFactService
from yuki.memory.worker import MemoryWorker
from yuki.persistence.models import (
    MemoryClaimCandidateEvidenceModel,
    MemoryClaimCandidateModel,
    MemoryFactModel,
    MemoryJobModel,
)
from yuki.persistence.repositories import EventLedgerRepository
from yuki.services.concurrency import ConcurrencyManager


@pytest.mark.parametrize("group_id", [None, "3001"])
@pytest.mark.parametrize("batch", [False, True])
async def test_actual_single_and_batch_requests_have_no_self_subject(database, group_id, batch):
    event = await _append_event(
        EventLedgerRepository(database), message_id="ordinary-scope", group_id=group_id
    )
    provider = FakeLLMProvider(lambda request: '{"claims":[]}')
    extractor = MemoryEventExtractor(InjectedModelExecutor(provider), ConcurrencyManager())
    if batch:
        await extractor.extract_batch((event,))
    else:
        await extractor.extract(event)
    payload = json.loads(provider.requests[0].messages[-1].content)
    declarations = (
        payload["events"][0]["available_subjects"] if batch else payload["available_subjects"]
    )
    assert "speaker" in {item["subject_ref"] for item in declarations}
    assert all(item["subject_ref"] != "self" for item in declarations)
    assert {scope for item in declarations for scope in item["allowed_scopes"]} == (
        {"person", "person_group", "group"} if group_id else {"person"}
    )
    assert "subject_ref=self" not in provider.requests[0].messages[0].content


async def test_worker_still_creates_person_person_group_and_group_from_original_event(database):
    ledger = EventLedgerRepository(database)
    event = await _append_event(
        ledger,
        message_id="ordinary-three-scopes",
        group_id="3001",
        content="我准备考研，本群称我琴友，本群周末一起练琴",
    )
    claims = [
        _claim(),
        _claim(
            scope_type="person_group",
            memory_key="group:nickname",
            content="群内称为琴友",
            evidence_quote="本群称我琴友",
        ),
        _claim(
            scope_type="group",
            subject_ref="group",
            memory_key="group:activity",
            content="本群周末一起练琴",
            evidence_quote="本群周末一起练琴",
        ),
    ]
    provider = FakeLLMProvider(
        lambda request: json.dumps(
            {
                "claims": [
                    {"source_event_id": event.id, "claim": claim.model_dump(mode="json")}
                    for claim in claims
                ]
            }
        )
    )
    jobs = MemoryJobRepository(database)
    worker = MemoryWorker(
        settings=make_settings(database.url, memory_batch_max_wait_seconds=0),
        jobs=jobs,
        facts=MemoryFactService(MemoryFactRepository(database)),
        ledger=ledger,
        model_executor=InjectedModelExecutor(provider),
        concurrency=ConcurrencyManager(),
    )
    assert await worker.enqueue(event.id, event.scope.key)
    assert await worker.process_once() == 1
    async with database.read_session() as reader:
        facts = (await reader.scalars(select(MemoryFactModel))).all()
        assert {fact.scope_type for fact in facts} == {"person", "person_group", "group"}
        job = await reader.scalar(select(MemoryJobModel).where(MemoryJobModel.event_id == event.id))
        assert job.outcome == MemoryRebuildJobOutcome.CLAIMS_APPLIED
        assert await reader.scalar(select(func.count()).select_from(MemoryClaimCandidateModel)) == 0


@pytest.mark.parametrize("self_attempt", [False, True])
async def test_worker_preserves_old_candidates_and_outcome_without_new_self_writes(
    database, self_attempt
):
    ledger = EventLedgerRepository(database)
    original = await _append_event(
        ledger, message_id="historical-self-source", content="Yuki喜欢冷笑话"
    )
    jobs = MemoryJobRepository(database)
    assert await jobs.enqueue(original.id, original.scope.key)
    (old_job,) = await jobs.claim()
    await jobs.complete(old_job, outcome=MemoryRebuildJobOutcome.CANDIDATES_STAGED)
    now = datetime.now(UTC)
    # A real pre-retirement row format, not an invocation of the retired producer.
    async with database.immediate_session() as writer:
        candidate = MemoryClaimCandidateModel(
            fingerprint="a" * 64,
            candidate_type="self",
            target_scope="self",
            subject_user_id="1001",
            group_id=None,
            target_fingerprint="b" * 64,
            normalized_memory_key="humor:preference",
            content="喜欢冷笑话",
            subject_basis="",
            retention="",
            source_style="",
            confidence=0.8,
            status="pending",
            evidence_count=1,
            expires_at=now + timedelta(days=7),
            created_at=now,
            updated_at=now,
        )
        writer.add(candidate)
        await writer.flush()
        candidate_id = candidate.id
        writer.add(
            MemoryClaimCandidateEvidenceModel(
                candidate_id=candidate_id, event_id=original.id, created_at=now
            )
        )
    async with database.read_session() as reader:
        before = (await reader.execute(select(MemoryClaimCandidateModel.__table__))).all()
        evidence_before = (
            await reader.execute(select(MemoryClaimCandidateEvidenceModel.__table__))
        ).all()
    event = await _append_event(ledger, message_id="new-person-source")
    claim = _claim().model_dump(mode="json")
    if self_attempt:
        claim.update(subject_ref="self", scope_type="self")
    provider = FakeLLMProvider(
        lambda request: json.dumps({"claims": [{"source_event_id": event.id, "claim": claim}]})
    )
    worker = MemoryWorker(
        settings=make_settings(database.url, memory_batch_max_wait_seconds=0),
        jobs=jobs,
        facts=MemoryFactService(MemoryFactRepository(database)),
        ledger=ledger,
        model_executor=InjectedModelExecutor(provider),
        concurrency=ConcurrencyManager(),
    )
    assert await worker.enqueue(event.id, event.scope.key)
    assert await worker.process_once() == (0 if self_attempt else 1)
    assert len(provider.requests) == 1
    async with database.read_session() as reader:
        assert (await reader.execute(select(MemoryClaimCandidateModel.__table__))).all() == before
        assert (
            await reader.execute(select(MemoryClaimCandidateEvidenceModel.__table__))
        ).all() == evidence_before
        assert (await reader.get(MemoryJobModel, old_job.id)).outcome == "candidates_staged"
        assert (
            await reader.scalar(
                select(func.count())
                .select_from(MemoryFactModel)
                .where(MemoryFactModel.scope_type == "self")
            )
            == 0
        )
        assert await reader.scalar(select(func.count()).select_from(MemoryFactModel)) == (
            0 if self_attempt else 1
        )
    assert (await ledger.get_event(original.id)).content == "Yuki喜欢冷笑话"
