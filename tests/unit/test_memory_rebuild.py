"""Controlled historical rebuild state machine, ordering, and safety contracts."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select, update
from tests.conftest import make_settings

from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.domain.messages import ChatRequest, ChatResponse
from qq_ai_bot.llm.base import LLMProvider
from qq_ai_bot.memory.enums import (
    MemoryRebuildCommitStatus,
    MemoryRebuildExpiredClaimPolicy,
    MemoryRebuildRunStatus,
    MemoryScopeType,
    MemorySourceType,
    MemoryStatus,
)
from qq_ai_bot.memory.models import MemoryFactCreate, MemoryFactQuery
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
    MemoryFactModel,
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


class _SlowExtractionProvider(_ExtractionProvider):
    def __init__(self) -> None:
        super().__init__()
        self.active = 0
        self.maximum_active = 0

    async def complete(self, request: ChatRequest) -> ChatResponse:
        self.active += 1
        self.maximum_active = max(self.maximum_active, self.active)
        try:
            await asyncio.sleep(0.02)
            return await super().complete(request)
        finally:
            self.active -= 1


class _FailOnceExtractionProvider(_ExtractionProvider):
    def __init__(self) -> None:
        super().__init__()
        self.failed = False

    async def complete(self, request: ChatRequest) -> ChatResponse:
        if not self.failed:
            self.failed = True
            raise RuntimeError("temporary extraction failure")
        return await super().complete(request)


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
        provider=provider,
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


def test_selection_requires_explicit_all_or_a_real_bound() -> None:
    with pytest.raises(ValidationError, match="range criterion"):
        MemoryRebuildSelection()
    assert MemoryRebuildSelection(all_events=True).all_events
    assert MemoryRebuildSelection(sender_user_ids=("1001",)).sender_user_ids == ("1001",)
    with pytest.raises(ValidationError, match="range criterion"):
        MemoryRebuildSelection(maximum_events=10)
    canonical = MemoryRebuildSelection(
        sender_user_ids=("2002", "1001"),
        scope_types=(ScopeType.PRIVATE, ScopeType.GROUP),
    )
    assert canonical.sender_user_ids == ("1001", "2002")
    assert canonical.scope_types == (ScopeType.GROUP, ScopeType.PRIVATE)


@pytest.mark.asyncio
async def test_plan_is_model_free_snapshot_and_uses_shared_eligibility(database: Database) -> None:
    _settings, ledger, _facts, provider, service = await _service(database)
    eligible = await _event(ledger, message_id="eligible")
    await _event(ledger, message_id="blank", content="   ")
    await ledger.append(
        bot_user_id="8000",
        platform_message_id="outbound",
        scope_type=ScopeType.PRIVATE,
        sender_user_id="8000",
        direction="outbound",
        content="bot text",
        private_peer_user_id="1001",
        sender_is_bot=True,
    )
    run = await service.plan(MemoryRebuildSelection(all_events=True), actor_user_id="9000")
    assert provider.requests == 0
    assert run.snapshot_max_event_id >= eligible.id
    assert run.plan_statistics.eligible_events == 1
    async with database.sessions() as session:
        assert (
            int(await session.scalar(select(func.count()).select_from(MemoryFactModel)) or 0) == 0
        )
        assert (
            int(await session.scalar(select(func.count()).select_from(MemoryRebuildItemModel)) or 0)
            == 0
        )


@pytest.mark.asyncio
async def test_rebuild_requires_review_then_commits_one_receipt(database: Database) -> None:
    settings, ledger, facts, provider, service = await _service(database)
    await _event(ledger, message_id="history")
    run = await service.plan(MemoryRebuildSelection(all_events=True), actor_user_id="9000")
    await service.start(run.public_id, actor_user_id="9000")
    worker = MemoryRebuildWorker(
        service, interval_seconds=settings.memory_rebuild_worker_interval_seconds
    )
    assert await worker.process_once() == 1
    assert await worker.process_once() == 0
    assert (await service.repository.get_run(run.public_id)).status is MemoryRebuildRunStatus.REVIEW
    with pytest.raises(ValueError, match="approved or rejected"):
        await service.commit(run.public_id, actor_user_id="9000")
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


@pytest.mark.asyncio
async def test_historical_confirmation_never_moves_confirmation_time_back(
    database: Database,
) -> None:
    settings, ledger, facts, _provider, service = await _service(database)
    old_time = datetime.now(UTC) - timedelta(days=30)
    await _event(ledger, message_id="old", content="我住在杭州", occurred_at=old_time)
    current = await facts.remember(
        MemoryFactCreate(
            scope_type=MemoryScopeType.PERSON,
            subject_user_id="1001",
            memory_key="profile:statement",
            category="profile",
            content="我住在杭州",
            source_type=MemorySourceType.AUTOMATIC,
        )
    )
    run = await service.plan(MemoryRebuildSelection(all_events=True), actor_user_id="9000")
    await service.start(run.public_id, actor_user_id="9000")
    worker = MemoryRebuildWorker(
        service, interval_seconds=settings.memory_rebuild_worker_interval_seconds
    )
    await worker.process_once()
    await worker.process_once()
    staged_rows = await service.review(run.public_id, actor_user_id="9000")
    assert len(staged_rows) == 1
    assert await service.set_review(run.public_id, "all", approved=True, actor_user_id="9000") == 1
    await service.commit(run.public_id, actor_user_id="9000")
    assert len(await service.repository.next_commit_rows(run.public_id, limit=10)) == 1
    await worker.process_once()
    refreshed = await facts.get_fact(current.id)
    assert refreshed is not None
    assert refreshed.last_confirmed_at >= current.last_confirmed_at


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
async def test_trusted_legacy_subject_metadata_never_crosses_group(database: Database) -> None:
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


@pytest.mark.asyncio
async def test_complete_v2_hydrate_rebuild_subjects_drops_other_presence(
    database: Database,
) -> None:
    from qq_ai_bot.identity.db_models import IdentityRuntimeStateModel
    from qq_ai_bot.identity.dual_write import (
        ensure_canonical_presence_preconfig as ensure_v2_presence,
    )
    from qq_ai_bot.identity.dual_write import ensure_v2_space
    from qq_ai_bot.persistence.models import ChatEventModel

    _settings, ledger, _facts, _provider, service = await _service(database)
    assert service is not None
    now = datetime.now(UTC)
    async with database.sessions() as session, session.begin():
        runtime = await session.get(IdentityRuntimeStateModel, 1)
        assert runtime is not None
        runtime.state = "v2"
        runtime.cutover_id = "550e8400-e29b-41d4-a716-446655440099"
        runtime.source_fingerprint = "cutover-fingerprint"
        runtime.completed_at = now
        await ensure_v2_presence(session, "8000")
        await ensure_v2_presence(session, "8001")
        await ensure_v2_space(session, "3001")
        session.add(
            ChatEventModel(
                bot_user_id="8000",
                platform_message_id="yuki-other",
                scope_type="group",
                group_id="3001",
                sender_user_id="8001",
                sender_nickname="",
                sender_group_card="",
                direction="inbound",
                event_kind="message",
                content="另一号说的",
                visual_summary="",
                segments_json="[]",
                origin="user_message",
                occurred_at=now,
                observed_at=now,
                author_kind="yuki",
                suppression_status="keeper",
            )
        )
        mention = ChatEventModel(
            bot_user_id="8000",
            platform_message_id="hydrate-v2",
            scope_type="group",
            group_id="3001",
            sender_user_id="1001",
            sender_nickname="",
            sender_group_card="",
            direction="inbound",
            event_kind="message",
            content="回另一号",
            visual_summary="",
            segments_json='[{"type":"at","data":{"qq":"8001"}}]',
            reply_to_message_id="yuki-other",
            origin="user_message",
            occurred_at=now,
            observed_at=now,
            author_kind="person",
            suppression_status="keeper",
        )
        session.add(mention)
    async with database.sessions() as session:
        mention = await session.scalar(
            select(ChatEventModel).where(ChatEventModel.platform_message_id == "hydrate-v2")
        )
        assert mention is not None
        event = await ledger.get_event(mention.id)
    assert event is not None
    hydrated = await ledger.hydrate_rebuild_subjects(event)
    assert hydrated.mentioned_user_ids == ()
    assert hydrated.reply_sender_user_id is None

    async with database.sessions() as session, session.begin():
        session.add(
            ChatEventModel(
                bot_user_id="8000",
                platform_message_id="ext-bot",
                scope_type="group",
                group_id="3001",
                sender_user_id="7777",
                sender_nickname="",
                sender_group_card="",
                direction="inbound",
                event_kind="message",
                content="机器人",
                visual_summary="",
                segments_json="[]",
                origin="user_message",
                occurred_at=now,
                observed_at=now,
                author_kind="external_bot",
                suppression_status="keeper",
            )
        )
        session.add(
            ChatEventModel(
                bot_user_id="8000",
                platform_message_id="sys-note",
                scope_type="group",
                group_id="3001",
                sender_user_id="0",
                sender_nickname="",
                sender_group_card="",
                direction="inbound",
                event_kind="message",
                content="系统",
                visual_summary="",
                segments_json="[]",
                origin="user_message",
                occurred_at=now,
                observed_at=now,
                author_kind="system",
                suppression_status="keeper",
            )
        )
        session.add(
            ChatEventModel(
                bot_user_id="8000",
                platform_message_id="hydrate-ext",
                scope_type="group",
                group_id="3001",
                sender_user_id="1001",
                sender_nickname="",
                sender_group_card="",
                direction="inbound",
                event_kind="message",
                content="回机器人",
                visual_summary="",
                segments_json='[{"type":"at","data":{"qq":"7777"}}]',
                reply_to_message_id="ext-bot",
                origin="user_message",
                occurred_at=now,
                observed_at=now,
                author_kind="person",
                suppression_status="keeper",
            )
        )
        session.add(
            ChatEventModel(
                bot_user_id="8000",
                platform_message_id="hydrate-sys",
                scope_type="group",
                group_id="3001",
                sender_user_id="1001",
                sender_nickname="",
                sender_group_card="",
                direction="inbound",
                event_kind="message",
                content="回系统",
                visual_summary="",
                segments_json="[]",
                reply_to_message_id="sys-note",
                origin="user_message",
                occurred_at=now,
                observed_at=now,
                author_kind="person",
                suppression_status="keeper",
            )
        )
    for platform_id in ("hydrate-ext", "hydrate-sys"):
        async with database.sessions() as session:
            row = await session.scalar(
                select(ChatEventModel).where(ChatEventModel.platform_message_id == platform_id)
            )
            assert row is not None
            record = await ledger.get_event(row.id)
        assert record is not None
        cleaned = await ledger.hydrate_rebuild_subjects(record)
        assert cleaned.mentioned_user_ids == ()
        assert cleaned.reply_sender_user_id is None


@pytest.mark.asyncio
async def test_historical_old_value_is_preserved_inactive(database: Database) -> None:
    settings, ledger, facts, _provider, service = await _service(
        database,
        person_memory_max_entries=1,
    )
    await _event(
        ledger,
        message_id="old-city",
        content="我住在福州",
        occurred_at=datetime.now(UTC) - timedelta(days=90),
    )
    current = await facts.remember(
        MemoryFactCreate(
            scope_type=MemoryScopeType.PERSON,
            subject_user_id="1001",
            memory_key="profile:statement",
            category="profile",
            content="我住在上海",
            source_type=MemorySourceType.AUTOMATIC,
        )
    )
    run = await service.plan(MemoryRebuildSelection(all_events=True), actor_user_id="9000")
    await service.start(run.public_id, actor_user_id="9000")
    worker = MemoryRebuildWorker(
        service, interval_seconds=settings.memory_rebuild_worker_interval_seconds
    )
    await worker.process_once()
    await worker.process_once()
    staged_rows = await service.review(run.public_id, actor_user_id="9000")
    assert len(staged_rows) == 1
    assert await service.set_review(run.public_id, "all", approved=True, actor_user_id="9000") == 1
    await service.commit(run.public_id, actor_user_id="9000")
    assert len(await service.repository.next_commit_rows(run.public_id, limit=10)) == 1
    assert await worker.process_once() == 1
    assert [row.id for row in await facts.list_person("1001")] == [current.id]
    historical = await facts.repository.list_facts(
        MemoryFactQuery(
            scope_type=MemoryScopeType.PERSON,
            subject_user_id="1001",
            status=MemoryStatus.SUPERSEDED,
        ),
        limit=10,
    )
    async with database.sessions() as session:
        proposal = await session.scalar(select(MemoryRebuildProposalModel))
        item = await session.scalar(select(MemoryRebuildItemModel))
    assert [row.content for row in historical] == ["我住在福州"]
    assert proposal is not None and proposal.actual_reason_code == "historical_version_preserved"
    assert item is not None and item.status == "committed"


@pytest.mark.asyncio
async def test_rebuild_never_evicts_current_fact_when_capacity_is_full(
    database: Database,
) -> None:
    settings, ledger, facts, _provider, service = await _service(
        database,
        person_memory_max_entries=1,
    )
    await _event(ledger, message_id="capacity", content="我喜欢远足")
    current = await facts.remember(
        MemoryFactCreate(
            scope_type=MemoryScopeType.PERSON,
            subject_user_id="1001",
            memory_key="protected:current",
            category="protected",
            content="当前重要事实",
            source_type=MemorySourceType.EXPLICIT,
        )
    )
    run = await service.plan(MemoryRebuildSelection(all_events=True), actor_user_id="9000")
    await service.start(run.public_id, actor_user_id="9000")
    worker = MemoryRebuildWorker(
        service, interval_seconds=settings.memory_rebuild_worker_interval_seconds
    )
    await worker.process_once()
    await worker.process_once()
    await service.set_review(run.public_id, "all", approved=True, actor_user_id="9000")
    await service.commit(run.public_id, actor_user_id="9000")
    assert await worker.process_once() == 1
    assert [row.id for row in await facts.list_person("1001")] == [current.id]
    async with database.sessions() as session:
        proposal = await session.scalar(select(MemoryRebuildProposalModel))
    assert proposal is not None
    assert proposal.actual_action == "noop"
    assert proposal.actual_reason_code == "rebuild_capacity_preserved"


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
async def test_review_filter_records_actor_and_is_idempotent(database: Database) -> None:
    settings, ledger, _facts, _provider, service = await _service(database)
    await _event(ledger, message_id="review-filter")
    run = await service.plan(MemoryRebuildSelection(all_events=True), actor_user_id="9000")
    await service.start(run.public_id, actor_user_id="9000")
    worker = MemoryRebuildWorker(
        service, interval_seconds=settings.memory_rebuild_worker_interval_seconds
    )
    await worker.process_once()
    await worker.process_once()
    selector = json.dumps({"subject": "1001", "confidence_min": 0.8})
    assert (
        await service.set_review(run.public_id, selector, approved=True, actor_user_id="9000") == 1
    )
    assert (
        await service.set_review(run.public_id, selector, approved=True, actor_user_id="9000") == 0
    )
    async with database.sessions() as session:
        proposal = await session.scalar(select(MemoryRebuildProposalModel))
    assert proposal is not None
    assert proposal.reviewed_by_user_id == "9000"
    assert proposal.reviewed_at is not None


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
    assert await service.forget_person("1001") >= 1
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
async def test_only_real_superuser_can_plan_or_list(database: Database) -> None:
    _settings, _ledger, _facts, provider, service = await _service(database)
    with pytest.raises(PermissionError, match="real superuser"):
        await service.plan(MemoryRebuildSelection(all_events=True), actor_user_id="1001")
    with pytest.raises(PermissionError, match="real superuser"):
        await service.list(actor_user_id="1001")
    assert provider.requests == 0


@pytest.mark.asyncio
async def test_extraction_concurrency_is_bounded_by_configuration(database: Database) -> None:
    provider = _SlowExtractionProvider()
    settings, ledger, _facts, _provider, service = await _service(
        database,
        provider=provider,
        memory_rebuild_extraction_concurrency=2,
        memory_rebuild_scan_batch_size=4,
    )
    for index in range(4):
        await _event(ledger, message_id=f"concurrent-{index}")
    run = await service.plan(MemoryRebuildSelection(all_events=True), actor_user_id="9000")
    await service.start(run.public_id, actor_user_id="9000")
    worker = MemoryRebuildWorker(
        service, interval_seconds=settings.memory_rebuild_worker_interval_seconds
    )
    assert await worker.process_once() == 4
    assert 1 <= provider.maximum_active <= 2


@pytest.mark.asyncio
async def test_extraction_failure_retries_with_persistent_backoff(database: Database) -> None:
    provider = _FailOnceExtractionProvider()
    settings, ledger, _facts, _provider, service = await _service(
        database,
        provider=provider,
        memory_rebuild_retry_initial_seconds=0.001,
    )
    await _event(ledger, message_id="retry-event")
    run = await service.plan(MemoryRebuildSelection(all_events=True), actor_user_id="9000")
    await service.start(run.public_id, actor_user_id="9000")
    worker = MemoryRebuildWorker(
        service, interval_seconds=settings.memory_rebuild_worker_interval_seconds
    )
    assert await worker.process_once() == 0
    await asyncio.sleep(0.01)
    assert await worker.process_once() == 1
    assert provider.failed
    async with database.sessions() as session:
        item = await session.scalar(select(MemoryRebuildItemModel))
    assert item is not None and item.attempts == 2 and item.status == "staged"
