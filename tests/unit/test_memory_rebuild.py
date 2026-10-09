"""Controlled historical rebuild state machine, ordering, and safety contracts."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, update
from tests.conftest import make_settings
from tests.support.model_executor import InjectedModelExecutor

from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.domain.messages import ChatRequest, ChatResponse
from qq_ai_bot.llm.base import LLMProvider, LLMUnavailableError
from qq_ai_bot.memory.enums import (
    MemoryRebuildCommitStatus,
    MemoryRebuildExpiredClaimPolicy,
    MemoryRebuildItemStatus,
    MemoryRebuildRunStatus,
    MemoryScopeType,
    MemorySourceType,
    MemoryStatus,
)
from qq_ai_bot.memory.models import MemoryFactQuery
from qq_ai_bot.memory.rebuild.models import MemoryRebuildSelection
from qq_ai_bot.memory.rebuild.repository import MemoryRebuildRepository
from qq_ai_bot.memory.rebuild.service import MemoryRebuildService
from qq_ai_bot.memory.rebuild.worker import MemoryRebuildWorker
from qq_ai_bot.memory.repository import MemoryFactRepository, MemoryJobRepository
from qq_ai_bot.memory.service import MemoryFactService
from qq_ai_bot.memory.worker import MemoryWorker
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.models import (
    ChatEventModel,
    MemoryJobModel,
    MemoryRebuildItemModel,
    MemoryRebuildProposalModel,
    MemoryRebuildRunModel,
)
from qq_ai_bot.persistence.repositories import EventLedgerRepository
from qq_ai_bot.services.concurrency import ConcurrencyManager


class _ExtractionProvider(LLMProvider):
    def __init__(self) -> None:
        self.requests = 0

    async def complete(self, request: ChatRequest) -> ChatResponse:
        self.requests += 1
        payload = json.loads(request.messages[-1].content or "{}")
        content = str(payload["primary_event"]["content"])
        claim: dict[str, object] = {
            "subject_ref": "speaker",
            "scope_type": "person",
            "kind": "fact",
            "memory_key": "profile:statement",
            "category": "profile",
            "content": content,
            "evidence_quote": content,
            "importance": 3,
            "confidence": 0.9,
            "source_type": "automatic",
        }
        if "临时" in content:
            claim.update(
                {
                    "temporal_mode": "temporary",
                    "valid_until": "2020-01-01T00:00:00+00:00",
                }
            )
        return ChatResponse(
            content=json.dumps(
                {"claims": [claim]},
                ensure_ascii=False,
            ),
            latency_seconds=0.125,
            prompt_tokens=11,
            completion_tokens=7,
        )


async def _service(
    database: Database,
    *,
    provider: _ExtractionProvider | None = None,
    **settings_overrides: object,
):
    settings = make_settings(
        database.url,
        memory_rebuild_enabled=True,
        memory_consolidation_enabled=False,
        **settings_overrides,
    )
    ledger = EventLedgerRepository(database)
    facts = MemoryFactService(MemoryFactRepository(database))
    provider = provider or _ExtractionProvider()
    live = MemoryWorker(
        settings=settings,
        jobs=MemoryJobRepository(database),
        facts=facts,
        ledger=ledger,
        model_executor=InjectedModelExecutor(provider),
        concurrency=ConcurrencyManager(2),
    )
    service = MemoryRebuildService(
        settings=settings,
        repository=MemoryRebuildRepository(database),
        ledger=ledger,
        extractor=live.extractor,
        processor=live.processor,
    )
    return settings, ledger, facts, provider, service


async def _event(
    ledger: EventLedgerRepository,
    *,
    message_id: str,
    content: str = "我住在杭州",
    occurred_at: datetime | None = None,
):
    event, _ = await ledger.append(
        bot_user_id="8000",
        platform_message_id=message_id,
        scope_type=ScopeType.PRIVATE,
        sender_user_id="1001",
        direction="inbound",
        content=content,
        private_peer_user_id="1001",
        occurred_at=occurred_at,
    )
    return event


@pytest.mark.asyncio
async def test_rebuild_requires_review_then_commits_one_receipt(database: Database) -> None:
    settings, ledger, facts, provider, service = await _service(database)
    await _event(ledger, message_id="history")
    selection = MemoryRebuildSelection(
        all_events=True,
        scope_types=(ScopeType.PRIVATE, ScopeType.PRIVATE),
        sender_user_ids=("1001", "1001"),
    )
    run = await service.plan(selection, actor_user_id="9000")
    await service.start(run.public_id, actor_user_id="9000")
    worker = MemoryRebuildWorker(
        service, interval_seconds=settings.memory_rebuild_worker_interval_seconds
    )
    assert await worker.process_once() == 1
    assert await worker.process_once() == 0
    assert (await service.repository.get_run(run.public_id)).status is MemoryRebuildRunStatus.REVIEW
    rows = await service.review(run.public_id, actor_user_id="9000")
    assert len(rows) == 1
    assert rows[0].source_excerpt == "我住在杭州"
    assert await service.set_review(run.public_id, "all", approved=True, actor_user_id="9000") == 1
    await service.commit(run.public_id, actor_user_id="9000")
    assert await worker.process_once() == 1
    assert (
        await service.repository.get_run(run.public_id)
    ).status is MemoryRebuildRunStatus.COMPLETED
    statistics = (await service.status(run.public_id, actor_user_id="9000"))["statistics"]
    assert statistics["extraction_requests"] == 1
    assert statistics["input_tokens"] == 11
    assert statistics["output_tokens"] == 7
    assert statistics["latency_milliseconds"] == 125
    stored = await facts.list_person("1001")
    assert len(stored) == 1 and stored[0].source_type is MemorySourceType.REBUILD
    async with database.sessions() as session:
        receipt = await session.scalar(select(MemoryJobModel))
        assert receipt is not None
        assert receipt.status == "done"
        assert receipt.processing_source == "rebuild"
        assert receipt.outcome == "claims_applied"
        assert (
            int(
                await session.scalar(select(func.count()).select_from(MemoryRebuildProposalModel))
                or 0
            )
            == 1
        )
    assert provider.requests == 1
    # Re-entering receipt preparation after completion must preserve the
    # original committed item/receipt instead of reporting our own job as a
    # conflicting "already_processed" live job.
    async with database.sessions() as session:
        item = await session.scalar(select(MemoryRebuildItemModel))
        identity = (item.id, item.event_id, item.updated_at, receipt.id, receipt.updated_at)
    assert (
        await service.repository.complete_item_receipts(
            run.public_id, include_failed_live_jobs=False
        )
        == 0
    )
    async with database.sessions() as session:
        item = await session.scalar(select(MemoryRebuildItemModel))
        receipt = await session.scalar(select(MemoryJobModel))
        assert item.status == MemoryRebuildItemStatus.COMMITTED.value
        assert item.error_category is None
        assert (item.id, item.event_id, item.updated_at, receipt.id, receipt.updated_at) == identity


@pytest.mark.asyncio
@pytest.mark.parametrize("maximum_events", [None, 1])
async def test_rebuild_selection_quantity_remains_the_requested_boundary(database, maximum_events):
    settings, ledger, _, provider, service = await _service(database)
    await _event(ledger, message_id="bounded-first")
    await _event(ledger, message_id="bounded-second")
    run = await service.plan(
        MemoryRebuildSelection(all_events=True, maximum_events=maximum_events), actor_user_id="9000"
    )
    await service.start(run.public_id, actor_user_id="9000")
    worker = MemoryRebuildWorker(
        service, interval_seconds=settings.memory_rebuild_worker_interval_seconds
    )
    expected = maximum_events or 2
    assert await worker.process_once() == expected
    assert await worker.process_once() == 0
    assert provider.requests == expected
    assert await service.repository.item_count(run.public_id) == expected
    assert (await service.repository.get_run(run.public_id)).status is MemoryRebuildRunStatus.REVIEW


@pytest.mark.asyncio
async def test_restart_pauses_without_resuming_or_calling_model(database: Database) -> None:
    settings, ledger, _facts, provider, service = await _service(database)
    await _event(ledger, message_id="restart")
    run = await service.plan(MemoryRebuildSelection(all_events=True), actor_user_id="9000")
    await service.start(run.public_id, actor_user_id="9000")
    worker = MemoryRebuildWorker(
        service, interval_seconds=settings.memory_rebuild_worker_interval_seconds
    )
    await worker.start()
    paused = await service.repository.get_run(run.public_id)
    assert paused is not None
    assert paused.status is MemoryRebuildRunStatus.EXTRACTION_PAUSED
    assert paused.error_category == "process_restart"
    assert provider.requests == 0
    await worker.close()


@pytest.mark.asyncio
async def test_snapshot_keyset_is_stable_and_excludes_later_event(database: Database) -> None:
    _settings, ledger, _facts, _provider, service = await _service(database)
    occurred_at = datetime.now(UTC) - timedelta(days=1)
    first = await _event(ledger, message_id="keyset-1", occurred_at=occurred_at)
    second = await _event(ledger, message_id="keyset-2", occurred_at=occurred_at)
    run = await service.plan(MemoryRebuildSelection(all_events=True), actor_user_id="9000")
    await _event(
        ledger,
        message_id="after-snapshot",
        occurred_at=occurred_at - timedelta(days=1),
    )
    page_one = await ledger.list_rebuild_candidates(
        run.selection,
        snapshot_max_event_id=run.snapshot_max_event_id,
        after_occurred_at=None,
        after_event_id=None,
        limit=1,
    )
    page_two = await ledger.list_rebuild_candidates(
        run.selection,
        snapshot_max_event_id=run.snapshot_max_event_id,
        after_occurred_at=page_one[-1].occurred_at,
        after_event_id=page_one[-1].id,
        limit=10,
    )
    assert [row.id for row in (*page_one, *page_two)] == [first.id, second.id]


@pytest.mark.asyncio
async def test_internal_reply_subject_metadata_never_crosses_group(database: Database) -> None:
    _settings, ledger, _facts, _provider, service = await _service(database)
    assert service is not None
    referenced, _ = await ledger.append(
        bot_user_id="8000",
        platform_message_id="reply-source",
        scope_type=ScopeType.GROUP,
        sender_user_id="2002",
        direction="inbound",
        content="原消息",
        group_id="3001",
    )
    event, _ = await ledger.append(
        bot_user_id="8000",
        platform_message_id="legacy-subjects",
        scope_type=ScopeType.GROUP,
        sender_user_id="1001",
        direction="inbound",
        content="确定性元数据",
        group_id="3001",
        reply_to_message_id=referenced.platform_message_id,
        reply_to_event_id=referenced.id,
        segments=({"type": "at", "data": {"qq": "3003"}},),
    )
    hydrated = await ledger.hydrate_rebuild_subjects(event)
    assert hydrated.mentioned_user_ids == ("3003",)
    assert hydrated.reply_sender_user_id == "2002"

    cross_group, _ = await ledger.append(
        bot_user_id="8000",
        platform_message_id="cross-group",
        scope_type=ScopeType.GROUP,
        sender_user_id="1001",
        direction="inbound",
        content="跨群回复",
        group_id="3002",
        reply_to_message_id=referenced.platform_message_id,
    )
    assert (await ledger.hydrate_rebuild_subjects(cross_group)).reply_sender_user_id is None

    old_event, _ = await ledger.append(
        bot_user_id="8000",
        platform_message_id="old-subjects",
        scope_type=ScopeType.GROUP,
        sender_user_id="1001",
        direction="inbound",
        content="旧引用缺内部编号",
        group_id="3001",
        reply_to_message_id=referenced.platform_message_id,
    )
    assert (await ledger.hydrate_rebuild_subjects(old_event)).reply_sender_user_id is None


@pytest.mark.asyncio
async def test_commit_rechecks_live_receipt_without_overwriting_it(database: Database) -> None:
    settings, ledger, _facts, _provider, service = await _service(database)
    event = await _event(ledger, message_id="receipt-race")
    run = await service.plan(MemoryRebuildSelection(all_events=True), actor_user_id="9000")
    await service.start(run.public_id, actor_user_id="9000")
    worker = MemoryRebuildWorker(
        service, interval_seconds=settings.memory_rebuild_worker_interval_seconds
    )
    await worker.process_once()
    await worker.process_once()
    await service.set_review(run.public_id, "all", approved=True, actor_user_id="9000")
    jobs = MemoryJobRepository(database)
    assert await jobs.enqueue(event.id, "private:1001")
    await service.commit(run.public_id, actor_user_id="9000")
    assert await worker.process_once() == 1
    async with database.sessions() as session:
        receipt = await session.scalar(
            select(MemoryJobModel).where(MemoryJobModel.event_id == event.id)
        )
        proposal = await session.scalar(select(MemoryRebuildProposalModel))
    assert receipt is not None and receipt.status == "pending"
    assert receipt.processing_source == "live"
    assert proposal is not None
    assert proposal.commit_status == MemoryRebuildCommitStatus.SKIPPED.value
    assert proposal.actual_reason_code == "live_job_active"


@pytest.mark.parametrize(
    ("policy", "expected_commit", "expected_fact_status"),
    (
        (MemoryRebuildExpiredClaimPolicy.SKIP, "skipped", None),
        (MemoryRebuildExpiredClaimPolicy.STAGE_INVALIDATED, "committed", MemoryStatus.INVALIDATED),
    ),
)
@pytest.mark.asyncio
async def test_expired_claim_policy_never_creates_an_active_fact(
    database: Database,
    policy: MemoryRebuildExpiredClaimPolicy,
    expected_commit: str,
    expected_fact_status: MemoryStatus | None,
) -> None:
    settings, ledger, facts, _provider, service = await _service(database)
    await _event(
        ledger,
        message_id=f"expired-{policy.value}",
        content="过去的临时状态",
        occurred_at=datetime(2019, 1, 1, tzinfo=UTC),
    )
    selection = MemoryRebuildSelection(all_events=True, expired_claim_policy=policy)
    run = await service.plan(selection, actor_user_id="9000")
    await service.start(run.public_id, actor_user_id="9000")
    worker = MemoryRebuildWorker(
        service, interval_seconds=settings.memory_rebuild_worker_interval_seconds
    )
    await worker.process_once()
    await worker.process_once()
    await service.set_review(run.public_id, "all", approved=True, actor_user_id="9000")
    await service.commit(run.public_id, actor_user_id="9000")
    assert await worker.process_once() == 1
    async with database.sessions() as session:
        proposal = await session.scalar(select(MemoryRebuildProposalModel))
    assert proposal is not None and proposal.commit_status == expected_commit
    assert await facts.list_person("1001") == ()
    if expected_fact_status is not None:
        rows = await facts.repository.list_facts(
            MemoryFactQuery(
                scope_type=MemoryScopeType.PERSON,
                subject_user_id="1001",
                status=expected_fact_status,
            ),
            limit=10,
        )
        assert len(rows) == 1


@pytest.mark.asyncio
async def test_commit_detects_source_fingerprint_change(database: Database) -> None:
    settings, ledger, facts, _provider, service = await _service(database)
    event = await _event(ledger, message_id="changed-source")
    run = await service.plan(MemoryRebuildSelection(all_events=True), actor_user_id="9000")
    await service.start(run.public_id, actor_user_id="9000")
    worker = MemoryRebuildWorker(
        service, interval_seconds=settings.memory_rebuild_worker_interval_seconds
    )
    await worker.process_once()
    await worker.process_once()
    await service.set_review(run.public_id, "all", approved=True, actor_user_id="9000")
    async with database.sessions() as session, session.begin():
        await session.execute(
            update(ChatEventModel).where(ChatEventModel.id == event.id).values(content="已改变")
        )
    await service.commit(run.public_id, actor_user_id="9000")
    assert await worker.process_once() == 1
    assert await facts.list_person("1001") == ()
    async with database.sessions() as session:
        proposal = await session.scalar(select(MemoryRebuildProposalModel))
    assert proposal is not None
    assert proposal.commit_status == "skipped"
    assert proposal.actual_reason_code == "source_event_changed"


@pytest.mark.asyncio
async def test_forget_person_removes_staging_and_redacts_selection(database: Database) -> None:
    settings, ledger, _facts, _provider, service = await _service(database)
    await _event(ledger, message_id="privacy-staging")
    run = await service.plan(
        MemoryRebuildSelection(sender_user_ids=("1001",)),
        actor_user_id="9000",
    )
    await service.start(run.public_id, actor_user_id="9000")
    worker = MemoryRebuildWorker(
        service, interval_seconds=settings.memory_rebuild_worker_interval_seconds
    )
    await worker.process_once()
    await worker.process_once()
    assert await service.repository.forget_people(("1001",)) >= 1
    async with database.sessions() as session:
        proposal_count = int(
            await session.scalar(select(func.count()).select_from(MemoryRebuildProposalModel)) or 0
        )
        stored = await session.scalar(
            select(MemoryRebuildRunModel).where(MemoryRebuildRunModel.public_id == run.public_id)
        )
    assert proposal_count == 0
    assert stored is not None and stored.status == "cancelled"
    assert "1001" not in stored.selection_json


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["receipt", "unknown_commit"])
async def test_rebuild_commit_receipt_is_atomic_and_usage_cannot_replay(
    database: Database, monkeypatch, failure: str
) -> None:
    from unittest.mock import AsyncMock

    _settings, ledger, facts, _provider, service = await _service(database)
    await _event(ledger, message_id="atomic-rebuild")
    run = await service.plan(MemoryRebuildSelection(all_events=True), actor_user_id="9000")
    await service.start(run.public_id, actor_user_id="9000")
    worker = MemoryRebuildWorker(service, interval_seconds=1)
    await worker.process_once()
    await worker.process_once()
    await service.set_review(run.public_id, "all", approved=True, actor_user_id="9000")
    await service.commit(run.public_id, actor_user_id="9000")
    resolve_spy = AsyncMock(wraps=service.processor.resolve)
    monkeypatch.setattr(service.processor, "resolve", resolve_spy)
    injected = False
    if failure == "receipt":
        original_finish = service.repository.finish_proposal

        async def finish(*args, **kwargs):
            nonlocal injected
            if not injected:
                injected = True
                raise RuntimeError("before receipt commit")
            await original_finish(*args, **kwargs)

        monkeypatch.setattr(service.repository, "finish_proposal", finish)
    else:
        original_write = facts.repository.apply_evidence_write

        async def uncertain(*args, **kwargs):
            nonlocal injected
            result = await original_write(*args, **kwargs)
            if not injected:
                injected = True
                raise RuntimeError("commit returned no acknowledgement")
            return result

        monkeypatch.setattr(facts.repository, "apply_evidence_write", uncertain)
    applied = await worker.process_once()
    assert await worker.process_once() == 0
    assert resolve_spy.await_count == 1
    async with database.sessions() as session:
        proposal = await session.scalar(select(MemoryRebuildProposalModel))
        if failure == "receipt":
            assert applied == 0 and await facts.list_person("1001") == ()
            assert proposal.commit_status == MemoryRebuildCommitStatus.PENDING.value
            assert proposal.actual_fact_id is None
        else:
            assert applied == 1 and len(await facts.list_person("1001")) == 1
            assert proposal.commit_status == MemoryRebuildCommitStatus.COMMITTED.value
            assert proposal.actual_fact_id is not None
        assert proposal.attempts == 1

    if failure == "receipt":
        async with database.sessions() as session, session.begin():
            await session.execute(
                update(MemoryRebuildProposalModel).values(
                    next_attempt_at=datetime.now(UTC) - timedelta(seconds=1)
                )
            )
        assert await worker.process_once() == 1
        assert len(await facts.list_person("1001")) == 1
        assert _provider.requests == 1
        assert await worker.process_once() == 0


@pytest.mark.asyncio
async def test_rebuild_database_failure_retains_proposal_until_recovery_without_repeating_model(
    database, monkeypatch
):
    from unittest.mock import AsyncMock

    from sqlalchemy.exc import OperationalError

    _settings, ledger, facts, _provider, service = await _service(database)
    await _event(ledger, message_id="persistent-database-failure")
    run = await service.plan(MemoryRebuildSelection(all_events=True), actor_user_id="9000")
    await service.start(run.public_id, actor_user_id="9000")
    worker = MemoryRebuildWorker(service, interval_seconds=1)
    await worker.process_once()
    await worker.process_once()
    await service.set_review(run.public_id, "all", approved=True, actor_user_id="9000")
    await service.commit(run.public_id, actor_user_id="9000")
    resolver = AsyncMock(wraps=service.processor.resolve)
    monkeypatch.setattr(service.processor, "resolve", resolver)
    database_write = AsyncMock(side_effect=OperationalError("write", {}, RuntimeError("disk")))
    original_write = facts.repository.apply_evidence_write
    monkeypatch.setattr(facts.repository, "apply_evidence_write", database_write)
    for _ in range(6):
        assert await worker.process_once() == 0
        async with database.sessions() as session, session.begin():
            await session.execute(
                update(MemoryRebuildProposalModel).values(
                    next_attempt_at=datetime.now(UTC) - timedelta(seconds=1)
                )
            )
    assert resolver.await_count == database_write.await_count == 6
    assert await facts.list_person("1001") == ()
    assert (
        await service.repository.get_run(run.public_id)
    ).status is MemoryRebuildRunStatus.COMMITTING
    async with database.sessions() as reader:
        proposal = await reader.scalar(select(MemoryRebuildProposalModel))
        assert proposal.commit_status == "pending"
        assert proposal.attempts == 6
    monkeypatch.setattr(facts.repository, "apply_evidence_write", original_write)
    assert await worker.process_once() == 1
    assert len(await facts.list_person("1001")) == 1
    assert _provider.requests == 1
    assert await worker.process_once() == 0


@pytest.mark.asyncio
async def test_rebuild_extraction_recovers_original_item_past_old_attempt_limit(database):
    class Provider(_ExtractionProvider):
        calls = 0

        async def complete(self, request: ChatRequest) -> ChatResponse:
            self.calls += 1
            if self.calls <= 6:
                raise LLMUnavailableError("temporary provider failure")
            return await super().complete(request)

    provider = Provider()
    _settings, ledger, _facts, _provider, service = await _service(database, provider=provider)
    source = await _event(ledger, message_id="repeated-rebuild-extraction")
    run = await service.plan(MemoryRebuildSelection(all_events=True), actor_user_id="9000")
    await service.start(run.public_id, actor_user_id="9000")
    worker = MemoryRebuildWorker(service, interval_seconds=1)
    for attempts in range(1, 7):
        assert await worker.process_once() == 0
        async with database.sessions() as session:
            item = await session.scalar(select(MemoryRebuildItemModel))
            assert item.id == 1 and item.event_id == source.id
            assert item.status == "pending" and item.attempts == attempts
        async with database.sessions() as session, session.begin():
            await session.execute(
                update(MemoryRebuildItemModel).values(
                    next_attempt_at=datetime.now(UTC) - timedelta(seconds=1)
                )
            )
    assert await worker.process_once() == 1
    async with database.sessions() as session:
        item = await session.scalar(select(MemoryRebuildItemModel))
        assert item.id == 1 and item.status == "staged" and item.attempts == 7
    assert provider.calls == 7
