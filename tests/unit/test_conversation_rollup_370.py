"""Adversarial coverage for the 3.7 single-checkpoint rollup contract."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from tests.conftest import make_settings

from qq_ai_bot.conversation.canonical_db_models import (
    CanonicalConversationRollupEmergencyOverlayModel as ConversationRollupEmergencyOverlayModel,
)
from qq_ai_bot.conversation.canonical_db_models import (
    CanonicalConversationRollupJobModel as ConversationRollupJobModel,
)
from qq_ai_bot.conversation.canonical_db_models import (
    CanonicalConversationRollupModel as ConversationRollupModel,
)
from qq_ai_bot.conversation.rollup.errors import (
    RollupLeaseLostError,
    RollupSourceChangedError,
)
from qq_ai_bot.conversation.rollup.models import (
    LLM_ORIGIN_INELIGIBLE,
    POLICY_PARK_DELAY,
    ConversationRollupDetailedStatus,
    ConversationScopeState,
    EmergencyOverlayDisposition,
    RollupCheckpointStatus,
    RollupCommitResult,
    RollupJobClaim,
    RollupKind,
    RollupPolicyConfig,
)
from qq_ai_bot.conversation.rollup.prompt_accounting import (
    durable_uncovered_characters,
    prompt_accounting_characters,
    prompt_visible_event_count,
)
from qq_ai_bot.conversation.rollup.renderer import (
    projection_characters,
    rollup_source_projection,
)
from qq_ai_bot.conversation.rollup.repository import (
    ConversationRollupRepository,
    ConversationScopeRepository,
    eligible_prefix,
    protected_tail_start,
)
from qq_ai_bot.conversation.rollup.service import ConversationRollupService
from qq_ai_bot.conversation.rollup.worker import ConversationRollupWorker
from qq_ai_bot.conversation.scope import ConversationTurnSnapshot
from qq_ai_bot.domain.conversations import ConversationScope, ScopeType
from qq_ai_bot.domain.messages import ChatResponse, InboundMessage, SenderIdentity
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.repository_records import EventRecord
from qq_ai_bot.persistence.scoped_event_uow import ScopedEventLedgerUnitOfWork
from qq_ai_bot.services.context_assembler import ContextAssembler, _HistoryPromptWindow


def test_rollup_source_projection_renders_stored_utc_in_default_timezone() -> None:
    event = EventRecord(
        id=32865,
        bot_user_id="380726517",
        platform_message_id="nailong-can",
        scope_type=ScopeType.GROUP,
        sender_user_id="3135003586",
        sender_group_card="查无此人",
        direction="inbound",
        content="这是什么",
        visual_summary="",
        segments=(),
        occurred_at=datetime(2026, 8, 20, 11, 21, 42, tzinfo=UTC),
        group_id="1049765710",
    )

    assert rollup_source_projection(event) == ("[2026-08-20T19:21:42+08:00] 查无此人: 这是什么")


def _reply_mention_events() -> tuple[EventRecord, ...]:
    occurred = datetime(2026, 8, 20, 11, 21, 42, tzinfo=UTC)
    parent = EventRecord(
        id=1,
        bot_user_id="380726517",
        platform_message_id="msg-parent",
        scope_type=ScopeType.GROUP,
        sender_user_id="10001",
        sender_group_card="Alice",
        direction="inbound",
        content="hello there",
        visual_summary="",
        segments=(),
        occurred_at=occurred,
        group_id="1049765710",
    )
    reply = EventRecord(
        id=2,
        bot_user_id="380726517",
        platform_message_id="msg-reply",
        scope_type=ScopeType.GROUP,
        sender_user_id="10002",
        sender_group_card="Bob",
        direction="inbound",
        content="got it",
        visual_summary="",
        segments=(),
        occurred_at=occurred + timedelta(seconds=1),
        group_id="1049765710",
        reply_to_message_id="msg-parent",
        mentioned_user_ids=("380726517",),
        reply_sender_user_id="10001",
    )
    return (parent, reply)


def test_prompt_accounting_matches_assembler_and_outweighs_projection() -> None:
    events = _reply_mention_events()
    prompt_chars = prompt_accounting_characters(events)
    projection_chars = sum(projection_characters(event) for event in events)
    assert projection_chars < prompt_chars

    settings = make_settings("sqlite+aiosqlite:///:memory:")
    assembler = ContextAssembler(
        settings=settings,
        ledger=MagicMock(),
        people=MagicMock(),
        memory_context=MagicMock(),
        relationships=MagicMock(),
        time_service=MagicMock(),
        rollup_repository=MagicMock(),
        rollup_service=MagicMock(),
    )
    dummy_current = EventRecord(
        id=99,
        bot_user_id="380726517",
        platform_message_id="msg-current",
        scope_type=ScopeType.GROUP,
        sender_user_id="10003",
        direction="inbound",
        content="now",
        visual_summary="",
        segments=(),
        occurred_at=datetime(2026, 8, 20, 11, 22, tzinfo=UTC),
        group_id="1049765710",
    )
    inbound = InboundMessage(
        message_id="msg-current",
        event_type="message",
        scope_type=ScopeType.GROUP,
        sender=SenderIdentity(user_id="10003", group_card="Carol"),
        text="now",
        bot_user_id="380726517",
        group_id="1049765710",
    )
    view = assembler._uncovered_prompt_view(
        events,
        current_event_id=dummy_current.id,
        content="now",
        yuki_account_ids=inbound.yuki_account_ids,
        current_message_override=None,
        current_event=dummy_current,
    )
    assert view is not None
    assert view.rendered_characters == prompt_chars


def _policy(*, batch_max_events: int = 100) -> RollupPolicyConfig:
    return RollupPolicyConfig(
        raw_tail_events=2,
        raw_tail_characters=100_000,
        trigger_events=2,
        trigger_characters=100_000,
        stop_events=0,
        stop_characters=0,
        batch_max_events=batch_max_events,
        batch_max_characters=100_000,
        summary_max_characters=2_000,
    )


async def _append(
    uow: ScopedEventLedgerUnitOfWork,
    scope: ConversationScope,
    count: int,
    *,
    start: int = 1,
    actor_prefix: str = "member",
    origin: str = "user_message",
) -> None:
    from qq_ai_bot.identity.canonical_repository import ensure_person, ensure_presence, ensure_space

    async with uow._database.sessions() as session, session.begin():
        await ensure_presence(session, scope.bot_user_id)
        if scope.scope_type is ScopeType.GROUP:
            assert scope.group_id is not None
            await ensure_space(session, scope.group_id)
        else:
            assert scope.private_peer_user_id is not None
            await ensure_person(session, scope.private_peer_user_id)
        for index in range(start, start + count):
            await ensure_person(session, f"{actor_prefix}-{index % 2}")
    for index in range(start, start + count):
        await uow.append(
            scope=scope,
            platform_message_id=f"message-{scope.bot_user_id}-{index}",
            sender_user_id=f"{actor_prefix}-{index % 2}",
            direction="inbound",
            content=f"event-{index}",
            occurred_at=datetime(2026, 8, 20, 0, index % 60, tzinfo=UTC),
            origin=origin,
        )


class _QualityFailModels:
    async def execute(self, *_args: object, **_kwargs: object) -> ChatResponse:
        return ChatResponse(content="", latency_seconds=0)


class _ProviderOsErrorModels:
    async def execute(self, *_args: object, **_kwargs: object) -> ChatResponse:
        raise OSError("provider exploded with exception text")


class _HangingModels:
    async def execute(self, *_args: object, **_kwargs: object) -> ChatResponse:
        await asyncio.Event().wait()
        raise AssertionError("hanging model resumed")


class _SuccessModels:
    async def execute(self, *_args: object, **_kwargs: object) -> ChatResponse:
        return ChatResponse(content="semantic catch-up summary", latency_seconds=0)


def _background_worker(
    repository: ConversationRollupRepository,
    service: ConversationRollupService,
    *,
    retry_max_seconds: int = 60,
) -> ConversationRollupWorker:
    return ConversationRollupWorker(
        repository=repository,
        service=service,
        enabled=True,
        concurrency=1,
        poll_seconds=0.05,
        lease_seconds=30,
        heartbeat_seconds=30,
        retry_max_seconds=retry_max_seconds,
        max_batches_per_claim=1,
        metrics=service.metrics,
    )


async def _run_worker_until(
    worker: ConversationRollupWorker,
    predicate,
    *,
    limit_seconds: float = 3.0,
) -> None:
    task = asyncio.create_task(worker._run("worker-loop"), name="rollup-worker-loop")
    try:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + limit_seconds
        while loop.time() < deadline:
            if await predicate():
                return
            await asyncio.sleep(0.02)
        raise AssertionError("worker loop condition was not met")
    finally:
        worker._stop.set()
        worker._wake.set()
        await asyncio.wait_for(task, timeout=2)


async def _force_rollup_job_due(
    database: Database,
    *,
    scope_id: int | None = None,
    conversation_id: str | None = None,
) -> None:
    now = datetime.now(UTC)
    async with database.sessions() as session, session.begin():
        if conversation_id is not None:
            from qq_ai_bot.conversation.canonical_db_models import (
                CanonicalConversationRollupJobModel,
            )

            job = await session.get(CanonicalConversationRollupJobModel, conversation_id)
        else:
            job = await session.get(ConversationRollupJobModel, scope_id)
        assert job is not None
        job.status = "pending"
        job.lease_owner = None
        job.lease_token = None
        job.lease_until = None
        job.next_attempt_at = now


async def _seed_job_last_error(
    database: Database,
    *,
    scope_id: int | None = None,
    conversation_id: str | None = None,
    category: str = "stale_error",
) -> None:
    async with database.sessions() as session, session.begin():
        if conversation_id is not None:
            from qq_ai_bot.conversation.canonical_db_models import (
                CanonicalConversationRollupJobModel,
            )

            job = await session.get(CanonicalConversationRollupJobModel, conversation_id)
        else:
            job = await session.get(ConversationRollupJobModel, scope_id)
        assert job is not None
        job.last_error_category = category


async def _assert_worker_model_failure_overlay(
    database: Database,
    *,
    models: object | None,
    timeout_seconds: float,
    expected_error_category: str,
    v2: bool = False,
) -> None:
    policy = _policy()
    if v2:
        scope = await _prepare_v2_private(database, peer="1001")
        uow = ScopedEventLedgerUnitOfWork(database, config=policy)
        await _append_v2(uow, scope, 4)
    else:
        scope = ConversationScope.private("bot-a", "peer-model-fail")
        uow = ScopedEventLedgerUnitOfWork(database, config=policy)
        await _append(uow, scope, 4)
    repository = ConversationRollupRepository(database, policy)
    failing = ConversationRollupService(
        models=models,  # type: ignore[arg-type]
        config=policy,
        timeout_seconds=timeout_seconds,
    )
    before = await repository.load_prompt_snapshot(scope)
    assert before.overlay is None
    assert before.rewrite_pending is False
    worker = _background_worker(repository, failing)
    await _run_worker_until(
        worker,
        lambda: _overlay_ready(repository, scope),
        limit_seconds=3.0 if timeout_seconds >= 0.1 else 4.0,
    )
    snapshot = await repository.load_prompt_snapshot(scope)
    assert snapshot.overlay is not None
    assert snapshot.overlay.summary_kind is RollupKind.EMERGENCY
    assert snapshot.rollup is not None
    assert snapshot.rollup.summary_kind is RollupKind.EMERGENCY
    assert snapshot.rewrite_pending is True
    assert snapshot.effective_coverage == snapshot.overlay.covered_through_event_id
    assert snapshot.effective_coverage > before.effective_coverage
    assert snapshot.scope.last_event_id == before.scope.last_event_id
    assert 0 < len(snapshot.overlay.summary_text) <= policy.summary_max_characters
    assert failing.metrics.coverage_commits == 0
    assert failing.metrics.model_summaries == 0
    assert failing.metrics.extractive_fallbacks == 1
    assert failing.metrics.infrastructure_retries == 0
    state, _effective, job = await repository.status(scope)
    assert state is not None and state.uncovered_event_count == 4
    assert job is not None and job["status"] == "pending"
    assert job["last_error_category"] == expected_error_category
    assert job["failure_count"] == 1
    async with database.sessions() as session:
        if v2:
            from sqlalchemy import select

            from qq_ai_bot.conversation.canonical_db_models import (
                CanonicalConversationRollupEmergencyOverlayModel,
                CanonicalConversationRollupJobModel,
                CanonicalConversationRollupModel,
                ConversationLegacyAliasModel,
            )

            alias = await session.scalar(
                select(ConversationLegacyAliasModel).where(
                    ConversationLegacyAliasModel.scope_key == scope.key
                )
            )
            assert alias is not None
            semantic = await session.get(CanonicalConversationRollupModel, alias.conversation_id)
            overlay = await session.get(
                CanonicalConversationRollupEmergencyOverlayModel, alias.conversation_id
            )
            stored_job = await session.get(
                CanonicalConversationRollupJobModel, alias.conversation_id
            )
            conversation_id = alias.conversation_id
            scope_id = None
        else:
            semantic = await session.get(ConversationRollupModel, snapshot.scope.id)
            overlay = await session.get(ConversationRollupEmergencyOverlayModel, snapshot.scope.id)
            stored_job = await session.get(ConversationRollupJobModel, snapshot.scope.id)
            conversation_id = None
            scope_id = snapshot.scope.id
        assert semantic is None
        assert overlay is not None
        assert stored_job is not None
        assert stored_job.status == "pending"
        assert stored_job.last_error_category == expected_error_category
        assert stored_job.failure_count == 1
        next_at = stored_job.next_attempt_at
        if next_at.tzinfo is None:
            next_at = next_at.replace(tzinfo=UTC)
        assert next_at > datetime.now(UTC)
    await _force_rollup_job_due(database, scope_id=scope_id, conversation_id=conversation_id)
    success = ConversationRollupService(
        models=_SuccessModels(),
        config=policy,
        timeout_seconds=1,
    )
    catchup = _background_worker(repository, success)
    await _run_worker_until(
        catchup,
        lambda: _overlay_cleared(repository, scope),
    )
    caught = await repository.load_prompt_snapshot(scope)
    assert caught.overlay is None
    assert caught.rewrite_pending is False
    assert caught.rollup is not None
    assert caught.rollup.summary_kind is RollupKind.MODEL
    assert caught.rollup.summary_text == "semantic catch-up summary"
    assert caught.effective_coverage == caught.rollup.covered_through_event_id
    assert success.metrics.coverage_commits == 1
    assert success.metrics.extractive_fallbacks == 0
    _caught_state, _caught_effective, caught_job = await repository.status(scope)
    if caught_job is not None:
        assert caught_job["last_error_category"] is None
    async with database.sessions() as session:
        if v2:
            from sqlalchemy import select

            from qq_ai_bot.conversation.canonical_db_models import (
                CanonicalConversationRollupJobModel,
                ConversationLegacyAliasModel,
            )

            alias = await session.scalar(
                select(ConversationLegacyAliasModel).where(
                    ConversationLegacyAliasModel.scope_key == scope.key
                )
            )
            assert alias is not None
            stored_job = await session.get(
                CanonicalConversationRollupJobModel, alias.conversation_id
            )
        else:
            stored_job = await session.get(ConversationRollupJobModel, snapshot.scope.id)
        if stored_job is not None:
            assert stored_job.last_error_category is None


async def _overlay_ready(
    repository: ConversationRollupRepository, scope: ConversationScope
) -> bool:
    snapshot = await repository.load_prompt_snapshot(scope)
    return snapshot.overlay is not None


async def _overlay_cleared(
    repository: ConversationRollupRepository, scope: ConversationScope
) -> bool:
    snapshot = await repository.load_prompt_snapshot(scope)
    return snapshot.overlay is None and snapshot.rollup is not None


async def test_worker_model_unavailable_writes_canonical_emergency_overlay(
    database: Database,
) -> None:
    await _assert_worker_model_failure_overlay(
        database,
        models=None,
        timeout_seconds=0.1,
        expected_error_category="RuntimeError",
        v2=True,
    )


async def test_visual_projection_change_rejects_locked_candidate(database: Database) -> None:
    policy = _policy()
    uow = ScopedEventLedgerUnitOfWork(database, config=policy)
    repository = ConversationRollupRepository(database, policy)
    scope = ConversationScope.private("bot-a", "peer-visual")
    await _append(uow, scope, 4)
    claim = await repository.claim_next_job(lease_owner="worker", lease_seconds=30)
    assert claim is not None
    candidate = await repository.candidate_for_claim(claim)
    assert candidate is not None

    await uow.set_visual_summary(candidate.events[0].id, "a newly available visual description")

    with pytest.raises(RollupSourceChangedError):
        await repository.commit_candidate(
            claim,
            candidate,
            summary_text="stale summary",
            summary_kind=RollupKind.MODEL,
        )


async def test_append_after_candidate_keeps_candidate_valid_and_preserves_signal(
    database: Database,
) -> None:
    policy = _policy()
    uow = ScopedEventLedgerUnitOfWork(database, config=policy)
    repository = ConversationRollupRepository(database, policy)
    scope = ConversationScope.private("bot-a", "peer-append")
    await _append(uow, scope, 4)
    claim = await repository.claim_next_job(lease_owner="worker", lease_seconds=30)
    assert claim is not None
    candidate = await repository.candidate_for_claim(claim)
    assert candidate is not None

    await _append(uow, scope, 1, start=5)
    await repository.commit_candidate(
        claim,
        candidate,
        summary_text="valid locked prefix",
        summary_kind=RollupKind.EXTRACTIVE,
    )

    _state, _rollup, job = await repository.status(scope)
    assert job is not None
    assert job["status"] == "pending"
    assert job["signal_revision"] == claim.claimed_signal_revision + 1


async def test_two_database_instances_only_claim_one_job(database: Database) -> None:
    policy = _policy()
    uow = ScopedEventLedgerUnitOfWork(database, config=policy)
    scope = ConversationScope.group("bot-a", "group-concurrent")
    await _append(uow, scope, 4)
    second_database = Database(database.url)
    try:
        first = ConversationRollupRepository(database, policy)
        second = ConversationRollupRepository(second_database, policy)
        claims = await asyncio.gather(
            first.claim_next_job(lease_owner="first", lease_seconds=30),
            second.claim_next_job(lease_owner="second", lease_seconds=30),
        )
    finally:
        await second_database.close()

    assert sum(claim is not None for claim in claims) == 1


async def test_expired_lease_same_owner_gets_new_token_and_old_token_is_rejected(
    database: Database,
) -> None:
    policy = _policy()
    uow = ScopedEventLedgerUnitOfWork(database, config=policy)
    repository = ConversationRollupRepository(database, policy)
    await _append(uow, ConversationScope.private("bot-a", "peer-lease"), 4)
    old = await repository.claim_next_job(lease_owner="same-owner", lease_seconds=30)
    assert old is not None and old.conversation_id is not None
    async with database.immediate_session() as session:
        job = await session.get(ConversationRollupJobModel, old.conversation_id)
        assert job is not None
        job.lease_until = datetime.now(UTC) - timedelta(seconds=1)

    new = await repository.claim_next_job(lease_owner="same-owner", lease_seconds=30)
    assert new is not None and new.lease_token != old.lease_token
    with pytest.raises(RollupLeaseLostError):
        await repository.heartbeat(old, lease_seconds=30)


async def test_successful_batch_resets_persisted_failure_before_next_retry(
    database: Database,
) -> None:
    policy = _policy(batch_max_events=1)
    uow = ScopedEventLedgerUnitOfWork(database, config=policy)
    repository = ConversationRollupRepository(database, policy)
    scope = ConversationScope.private("bot-a", "peer-retry")
    await _append(uow, scope, 6)
    claim = await repository.claim_next_job(lease_owner="worker", lease_seconds=30)
    assert claim is not None and claim.conversation_id is not None
    async with database.immediate_session() as session:
        stored = await session.get(ConversationRollupJobModel, claim.conversation_id)
        assert stored is not None
        stored.failure_count = 4
    # Reclaim so the claim reflects the old failure count, then prove a successful
    # retained batch resets the persisted value used by the next infrastructure retry.
    async with database.immediate_session() as session:
        stored = await session.get(ConversationRollupJobModel, claim.conversation_id)
        assert stored is not None
        stored.lease_until = datetime.now(UTC) - timedelta(seconds=1)
    claim = await repository.claim_next_job(lease_owner="worker", lease_seconds=30)
    assert claim is not None and claim.failure_count == 4 and claim.conversation_id is not None
    candidate = await repository.candidate_for_claim(claim)
    assert candidate is not None
    committed = await repository.commit_candidate(
        claim,
        candidate,
        summary_text="first successful batch",
        summary_kind=RollupKind.EXTRACTIVE,
        retain_lease=True,
    )
    assert committed.claim_retained is True
    await repository.retry_infrastructure(
        claim,
        error_category="database_unavailable",
        retry_max_seconds=960,
    )
    _state, _rollup, job = await repository.status(scope)
    assert job is not None and job["failure_count"] == 1


async def test_foreground_claim_prevents_background_result_overwrite(database: Database) -> None:
    policy = _policy()
    uow = ScopedEventLedgerUnitOfWork(database, config=policy)
    repository = ConversationRollupRepository(database, policy)
    scope = ConversationScope.private("bot-a", "peer-preempt")
    await _append(uow, scope, 4)
    background = await repository.claim_next_job(lease_owner="background", lease_seconds=30)
    assert background is not None
    stale_candidate = await repository.candidate_for_claim(background)
    assert stale_candidate is not None
    foreground = await repository.claim_scope_for_foreground(
        scope,
        lease_owner="foreground",
        lease_seconds=30,
    )
    assert foreground is not None
    current_candidate = await repository.candidate_for_claim(foreground)
    assert current_candidate is not None
    await repository.commit_candidate(
        foreground,
        current_candidate,
        summary_text="foreground wins",
        summary_kind=RollupKind.EXTRACTIVE,
    )

    with pytest.raises(RollupLeaseLostError):
        await repository.commit_candidate(
            background,
            stale_candidate,
            summary_text="stale background",
            summary_kind=RollupKind.MODEL,
        )


async def test_single_protected_event_never_creates_permanent_job(database: Database) -> None:
    policy = RollupPolicyConfig(
        raw_tail_events=1,
        raw_tail_characters=1,
        trigger_events=2,
        trigger_characters=2,
        stop_events=0,
        stop_characters=0,
        batch_max_events=10,
        batch_max_characters=10,
        summary_max_characters=100,
    )
    uow = ScopedEventLedgerUnitOfWork(database, config=policy)
    repository = ConversationRollupRepository(database, policy)
    scope = await _prepare_v2_private(database, bot="bot-a", peer="peer-long")
    await uow.append(
        scope=scope,
        platform_message_id="one-long-message",
        sender_user_id="peer-long",
        direction="inbound",
        content="x" * 10_000,
    )

    _state, _rollup, job = await repository.status(scope)
    assert job is None


def test_prompt_event_caps_use_trigger_and_stop_not_tail_plus_one() -> None:
    assembler = ContextAssembler(
        settings=make_settings("sqlite+aiosqlite:///:memory:"),
        ledger=MagicMock(),
        people=MagicMock(),
        memory_context=MagicMock(),
        relationships=MagicMock(),
        time_service=MagicMock(),
        rollup_repository=MagicMock(),
        rollup_service=MagicMock(),
    )

    assert assembler._prompt_event_admit(event_limit=2048, coverage_end=0) == 2047
    assert assembler._prompt_event_admit(event_limit=2048, coverage_end=100) == 512
    assert assembler._prompt_event_target(event_limit=2048, coverage_end=100) == 128
    assert assembler._prompt_event_admit(event_limit=1024, coverage_end=100) == 512


async def test_foreground_does_not_nibble_between_protected_tail_and_trigger(
    database: Database,
) -> None:
    settings = make_settings(
        database.url,
        local_context_event_limit=16,
        conversation_rollup_raw_tail_events=8,
        conversation_rollup_trigger_events=4,
        conversation_rollup_stop_events=2,
        conversation_rollup_raw_tail_characters=100_000,
        conversation_rollup_trigger_characters=100_000,
        conversation_rollup_stop_characters=10_000,
        conversation_rollup_batch_max_events=8,
        conversation_rollup_batch_max_characters=100_000,
        conversation_rollup_summary_max_characters=2_000,
        conversation_rollup_foreground_max_batches=4,
    )
    policy = RollupPolicyConfig(
        raw_tail_events=settings.conversation_rollup_raw_tail_events,
        raw_tail_characters=settings.conversation_rollup_raw_tail_characters,
        trigger_events=settings.conversation_rollup_trigger_events,
        trigger_characters=settings.conversation_rollup_trigger_characters,
        stop_events=settings.conversation_rollup_stop_events,
        stop_characters=settings.conversation_rollup_stop_characters,
        batch_max_events=settings.conversation_rollup_batch_max_events,
        batch_max_characters=settings.conversation_rollup_batch_max_characters,
        summary_max_characters=settings.conversation_rollup_summary_max_characters,
    )
    uow = ScopedEventLedgerUnitOfWork(database, config=policy)
    repository = ConversationRollupRepository(database, policy)
    service = ConversationRollupService(models=None, config=policy, timeout_seconds=0.1)
    assembler = ContextAssembler(
        settings=settings,
        ledger=MagicMock(),
        people=MagicMock(),
        memory_context=MagicMock(),
        relationships=MagicMock(),
        time_service=MagicMock(),
        rollup_repository=repository,
        rollup_service=service,
    )
    scope = ConversationScope.group("bot-hysteresis", "group-hysteresis")
    await _append(uow, scope, 12)
    claim = await repository.claim_next_job(lease_owner="seed", lease_seconds=30)
    assert claim is not None
    candidate = await repository.candidate_for_claim(claim)
    assert candidate is not None
    summary, _kind = service.extractive(candidate)
    await repository.commit_candidate(
        claim, candidate, summary_text=summary, summary_kind=RollupKind.EXTRACTIVE
    )
    seeded, seeded_rollup, _job = await repository.status(scope)
    assert seeded is not None and seeded_rollup is not None
    assert seeded.uncovered_event_count == 8
    seeded_revision = seeded_rollup.revision
    seeded_coverage = seeded_rollup.covered_through_event_id

    await _append(uow, scope, 3, start=13)
    dead_zone, dead_rollup, _job = await repository.status(scope)
    assert dead_zone is not None and dead_rollup is not None
    assert dead_zone.uncovered_event_count == 11
    await assembler._ensure_lightweight_backlog(
        scope,
        ConversationTurnSnapshot(
            scope_id=dead_zone.id,
            scope_key=dead_zone.scope.key,
            generation=dead_zone.generation,
            trigger_event_id=dead_zone.last_event_id,
            coordinator_version=1,
        ),
        event_limit=settings.local_context_event_limit,
    )
    after_dead, after_dead_rollup, _job = await repository.status(scope)
    assert after_dead is not None and after_dead_rollup is not None
    assert after_dead.uncovered_event_count == 11
    assert after_dead_rollup.revision == seeded_revision
    assert after_dead_rollup.covered_through_event_id == seeded_coverage

    await _append(uow, scope, 2, start=16)
    over_trigger, over_rollup, _job = await repository.status(scope)
    assert over_trigger is not None and over_rollup is not None
    assert over_trigger.uncovered_event_count == 13
    assert over_rollup.revision == seeded_revision
    committed = await service.ensure_extractive_coverage(
        repository=repository,
        scope=scope,
        lease_seconds=30,
        max_batches=4,
    )
    assert committed >= 1
    compacted, compacted_effective, _job = await repository.status(scope)
    assert compacted is not None and compacted_effective is not None
    assert compacted.uncovered_event_count == 13
    assert compacted_effective.summary_kind is RollupKind.EMERGENCY
    assert compacted_effective.covered_through_event_id > seeded_coverage
    snapshot = await repository.load_prompt_snapshot(scope)
    assert snapshot.rewrite_pending is True
    assert snapshot.overlay is not None
    assert snapshot.effective_coverage == compacted_effective.covered_through_event_id
    async with database.sessions() as session:
        semantic = await session.get(ConversationRollupModel, claim.conversation_id)
        assert semantic is not None
        assert semantic.revision == seeded_revision
        assert semantic.covered_through_event_id == seeded_coverage
        assert semantic.summary_kind == RollupKind.EXTRACTIVE.value


async def test_lightweight_backlog_triggers_on_prompt_ruler_not_projection(
    database: Database,
) -> None:
    policy = RollupPolicyConfig(
        raw_tail_events=2,
        raw_tail_characters=100_000,
        trigger_events=32,
        trigger_characters=100_000,
        stop_events=0,
        stop_characters=0,
        batch_max_events=8,
        batch_max_characters=100_000,
        summary_max_characters=2_000,
    )
    uow = ScopedEventLedgerUnitOfWork(database, config=policy)
    repository = ConversationRollupRepository(database, policy)
    service = ConversationRollupService(models=None, config=policy, timeout_seconds=0.1)
    scope = ConversationScope.group("bot-ruler", "group-ruler")
    from qq_ai_bot.identity.canonical_repository import ensure_person, ensure_presence, ensure_space

    async with database.sessions() as session, session.begin():
        await ensure_presence(session, scope.bot_user_id)
        await ensure_space(session, scope.group_id or "")
        await ensure_person(session, "10000")
        await ensure_person(session, "10001")
    first = None
    for index in range(1, 7):
        result = await uow.append(
            scope=scope,
            platform_message_id=f"msg-{index}",
            sender_user_id=f"1000{index % 2}",
            sender_group_card="Alice" if index % 2 else "Bob",
            direction="inbound",
            content="short",
            occurred_at=datetime(2026, 8, 20, 0, index, tzinfo=UTC),
            reply_to_message_id=None if index == 1 else f"msg-{index - 1}",
            segments=(
                (
                    {
                        "type": "yuki_context",
                        "data": {
                            "mentioned_user_ids": [scope.bot_user_id],
                            "reply_sender_user_id": f"1000{(index - 1) % 2}",
                        },
                    },
                )
                if index > 1
                else ()
            ),
        )
        first = first or result
    snapshot = await repository.load_prompt_snapshot(scope)
    projection = sum(projection_characters(event) for event in snapshot.raw_events)
    prompt = prompt_accounting_characters(
        snapshot.raw_events,
        bot_display_name=policy.bot_display_name,
        timezone=policy.timezone,
    )
    durable = durable_uncovered_characters(
        snapshot.raw_events,
        bot_display_name=policy.bot_display_name,
        timezone=policy.timezone,
    )
    remaining_prompt = prompt_accounting_characters(
        snapshot.raw_events[-2:],
        bot_display_name=policy.bot_display_name,
        timezone=policy.timezone,
    )
    admit = (projection + prompt) // 2
    assert projection < admit <= prompt
    assert remaining_prompt < admit
    async with database.immediate_session() as session:
        from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel
        from qq_ai_bot.conversation.rollup.repository import recount_canonical_uncovered

        row = await session.get(CanonicalConversationModel, first.event.canonical_conversation_id)
        assert row is not None
        row.uncovered_character_count = projection
        recounted = await recount_canonical_uncovered(session, row, policy)
    assert recounted[1] == durable
    seeded, seeded_rollup, _job = await repository.status(scope)
    assert seeded is not None
    assert seeded.uncovered_character_count == durable
    assert seeded_rollup is None
    committed = await service.ensure_extractive_coverage(
        repository=repository,
        scope=scope,
        lease_seconds=30,
        max_batches=4,
    )
    assert committed >= 1
    compacted, compacted_effective, _job = await repository.status(scope)
    assert compacted is not None
    assert compacted.uncovered_event_count == 6
    snapshot = await repository.load_prompt_snapshot(scope)
    assert snapshot.rewrite_pending is True
    assert snapshot.overlay is not None
    assert snapshot.effective_coverage > 0
    assert compacted_effective is not None
    assert compacted_effective.summary_kind is RollupKind.EMERGENCY
    async with database.sessions() as session:
        assert await session.get(ConversationRollupModel, compacted.id) is None


def _counted_events(count: int, *, body: str) -> tuple[EventRecord, ...]:
    occurred = datetime(2026, 8, 20, 0, 0, tzinfo=UTC)
    return tuple(
        EventRecord(
            id=index,
            bot_user_id="bot-floor",
            platform_message_id=f"msg-{index}",
            scope_type=ScopeType.GROUP,
            sender_user_id=f"1000{index % 2}",
            sender_group_card="Alice" if index % 2 else "Bob",
            direction="inbound",
            content=body,
            visual_summary="",
            segments=(),
            occurred_at=occurred + timedelta(seconds=index),
            group_id="group-floor",
        )
        for index in range(1, count + 1)
    )


def test_long_messages_raise_character_index_and_keep_eligible_prefix() -> None:
    policy = RollupPolicyConfig(
        raw_tail_events=4,
        raw_tail_characters=200,
        trigger_events=8,
        trigger_characters=100_000,
        stop_events=0,
        stop_characters=0,
        batch_max_events=8,
        batch_max_characters=100_000,
        summary_max_characters=2_000,
    )
    events = _counted_events(6, body="z" * 500)
    start = protected_tail_start(events, policy)
    protected = events[start:]
    assert prompt_visible_event_count(protected) == policy.raw_tail_events
    assert tuple(event.id for event in protected) == (3, 4, 5, 6)
    eligible = eligible_prefix(events, policy)
    assert eligible
    assert eligible[-1].id < events[start].id


async def test_event_floor_between_character_target_and_admit_skips_extractive() -> None:
    settings = make_settings(
        "sqlite+aiosqlite:///:memory:",
        local_context_event_limit=2048,
        conversation_rollup_raw_tail_events=256,
        conversation_rollup_trigger_events=1024,
        conversation_rollup_stop_events=0,
        conversation_rollup_raw_tail_characters=20_480,
        conversation_rollup_trigger_characters=81_920,
        conversation_rollup_stop_characters=0,
    )
    events = _counted_events(256, body="y" * 80)
    current = events[-1]
    history = events
    prompt = prompt_accounting_characters(history)
    target = (
        settings.conversation_rollup_raw_tail_characters
        + settings.conversation_rollup_stop_characters
    )
    admit = (
        settings.conversation_rollup_raw_tail_characters
        + settings.conversation_rollup_trigger_characters
    )
    assert target < prompt <= admit
    rollup_service = MagicMock()
    rollup_service.ensure_extractive_coverage = AsyncMock(
        side_effect=AssertionError("admit-window turns must not extractive")
    )
    assembler = ContextAssembler(
        settings=settings,
        ledger=MagicMock(),
        people=MagicMock(),
        memory_context=MagicMock(),
        relationships=MagicMock(),
        time_service=MagicMock(),
        rollup_repository=MagicMock(),
        rollup_service=rollup_service,
    )
    inbound = InboundMessage(
        message_id="msg-current",
        event_type="message",
        scope_type=ScopeType.GROUP,
        sender=SenderIdentity(user_id="10009", group_card="Carol"),
        text="now",
        bot_user_id="bot-floor",
        group_id="group-floor",
    )
    dummy_current = EventRecord(
        id=10_000,
        bot_user_id="bot-floor",
        platform_message_id="msg-current",
        scope_type=ScopeType.GROUP,
        sender_user_id="10009",
        sender_group_card="Carol",
        direction="inbound",
        content="now",
        visual_summary="",
        segments=(),
        occurred_at=datetime(2026, 8, 20, 1, 0, tzinfo=UTC),
        group_id="group-floor",
    )
    snapshot = _HistoryPromptWindow(
        recent=history,
        rollup_text="seed",
        coverage_end=1,
        revision=1,
        rollup=None,
        rollup_mode="extractive",
    )
    view = assembler._uncovered_prompt_view(
        history,
        current_event_id=dummy_current.id,
        content="now",
        yuki_account_ids=inbound.yuki_account_ids,
        current_message_override=None,
        current_event=dummy_current,
    )
    assert view is not None
    assert view.rendered_characters == prompt
    character_target = assembler._prompt_character_target(
        remainder=1_000_000,
        rollup_text="seed",
        coverage_end=1,
    )
    character_admit = assembler._prompt_character_admit(
        remainder=1_000_000,
        rollup_text="seed",
        coverage_end=1,
    )
    assert character_target < view.rendered_characters <= character_admit
    await assembler._ensure_uncovered_fits_budget(
        snapshot=snapshot,
        recent=history,
        current_event_id=dummy_current.id,
        content="now",
        yuki_account_ids=inbound.yuki_account_ids,
        current_message_override=None,
        remainder=1_000_000,
        event_limit=settings.local_context_event_limit,
        identity=ConversationScope.group("bot-floor", "group-floor"),
        current_event=dummy_current,
        turn=ConversationTurnSnapshot(
            scope_id=1,
            scope_key="bot:bot-floor:group:group-floor",
            generation=1,
            trigger_event_id=current.id,
            coordinator_version=1,
        ),
    )
    rollup_service.ensure_extractive_coverage.assert_not_called()


_NOW = datetime(2026, 8, 24, tzinfo=UTC)


async def _prepare_v2_private(
    database: Database, *, bot: str = "8000", peer: str = "1001"
) -> ConversationScope:
    from qq_ai_bot.identity.canonical_repository import ensure_person, ensure_presence

    async with database.sessions() as session, session.begin():
        await ensure_presence(session, bot)
        await ensure_person(session, peer, now=_NOW)
    return ConversationScope.private(bot, peer)


@pytest.mark.asyncio
async def test_v2_status_and_prompt_read_history_across_aliases(
    database: Database,
) -> None:
    from qq_ai_bot.conversation.hydrate import ensure_legacy_alias
    from qq_ai_bot.conversation.rollup.errors import ConversationCoverageError
    from qq_ai_bot.identity.canonical_repository import ensure_presence
    from qq_ai_bot.persistence.models import ChatEventModel

    primary = await _prepare_v2_private(database)
    secondary = ConversationScope.private("8001", "1001")
    policy = _policy()
    uow = ScopedEventLedgerUnitOfWork(database, config=policy)
    scopes = ConversationScopeRepository(database)
    repository = ConversationRollupRepository(database, policy)
    appended = await uow.append(
        scope=primary,
        platform_message_id="alias-hist-1",
        sender_user_id="1001",
        direction="inbound",
        content="first-on-primary",
    )
    assert appended.scope.runtime_scope_key == primary.key
    async with database.sessions() as session, session.begin():
        await ensure_presence(session, "8001")
        event = await session.get(ChatEventModel, appended.event.id)
        assert event is not None and event.canonical_conversation_id
        await ensure_legacy_alias(
            session,
            conversation_id=event.canonical_conversation_id,
            scope_key=secondary.key,
            primary=False,
        )
    loaded = await scopes.get(secondary)
    assert loaded is not None
    assert loaded.runtime_scope_key == primary.key
    assert loaded.scope.key == secondary.key
    assert loaded.id == appended.scope.id
    status_state, _rollup, _job = await repository.status(secondary)
    assert status_state is not None
    assert status_state.runtime_scope_key == primary.key
    snapshot = await repository.load_prompt_snapshot(secondary)
    assert snapshot.scope.runtime_scope_key == primary.key
    assert [row.content for row in snapshot.raw_events] == ["first-on-primary"]
    missing = ConversationScope.private("8000", "1999")
    assert await scopes.get(missing) is None
    with pytest.raises(ConversationCoverageError):
        await repository.load_prompt_snapshot(missing)


@pytest.mark.asyncio
async def test_v2_prompt_fence_accepts_secondary_and_rejects_forged_keys(
    database: Database,
) -> None:
    from qq_ai_bot.conversation.hydrate import ensure_legacy_alias
    from qq_ai_bot.conversation.rollup.errors import ConversationCoverageError
    from qq_ai_bot.identity.canonical_repository import ensure_presence
    from qq_ai_bot.persistence.models import ChatEventModel

    primary = await _prepare_v2_private(database)
    secondary = ConversationScope.private("8001", "1001")
    policy = _policy()
    uow = ScopedEventLedgerUnitOfWork(database, config=policy)
    repository = ConversationRollupRepository(database, policy)
    appended = await uow.append(
        scope=primary,
        platform_message_id="fence-1",
        sender_user_id="1001",
        direction="inbound",
        content="kept",
    )
    async with database.sessions() as session, session.begin():
        await ensure_presence(session, "8001")
        event = await session.get(ChatEventModel, appended.event.id)
        assert event is not None and event.canonical_conversation_id
        await ensure_legacy_alias(
            session,
            conversation_id=event.canonical_conversation_id,
            scope_key=secondary.key,
            primary=False,
        )
    settings = make_settings(database.url)
    assembler = ContextAssembler(
        settings=settings,
        ledger=MagicMock(),
        people=MagicMock(),
        memory_context=MagicMock(),
        relationships=MagicMock(),
        time_service=MagicMock(),
        rollup_repository=repository,
        rollup_service=MagicMock(),
    )
    turn = ConversationTurnSnapshot(
        scope_id=appended.scope.id,
        scope_key=primary.key,
        generation=appended.scope.generation,
        trigger_event_id=appended.event.id,
        coordinator_version=1,
        transport_scope_key=secondary.key,
    )
    window = await assembler._load_history_snapshot(secondary, turn=turn, before_event_id=None)
    assert [row.content for row in window.recent] == ["kept"]
    forged_primary = ConversationTurnSnapshot(
        scope_id=appended.scope.id,
        scope_key="bot:9999:private:1001",
        generation=appended.scope.generation,
        trigger_event_id=appended.event.id,
        coordinator_version=1,
        transport_scope_key=secondary.key,
    )
    with pytest.raises(ConversationCoverageError):
        await assembler._load_history_snapshot(secondary, turn=forged_primary, before_event_id=None)
    wrong_transport = ConversationTurnSnapshot(
        scope_id=appended.scope.id,
        scope_key=primary.key,
        generation=appended.scope.generation,
        trigger_event_id=appended.event.id,
        coordinator_version=1,
        transport_scope_key="bot:8002:private:1001",
    )
    with pytest.raises(ConversationCoverageError):
        await assembler._load_history_snapshot(
            secondary, turn=wrong_transport, before_event_id=None
        )


async def test_v2_foreground_claim_uses_canonical_job_not_scopes(database: Database) -> None:
    scope = await _prepare_v2_private(database)
    policy = _policy()
    uow = ScopedEventLedgerUnitOfWork(database, config=policy)
    repository = ConversationRollupRepository(database, policy)
    await uow.append(
        scope=scope,
        platform_message_id="v2-fg-1",
        sender_user_id="1001",
        direction="inbound",
        content="hello",
    )
    claim = await repository.claim_scope_for_foreground(
        scope, lease_owner="foreground", lease_seconds=30
    )
    assert claim is not None
    assert claim.conversation_id
    async with database.sessions() as session:
        from sqlalchemy import func, select

        from qq_ai_bot.conversation.canonical_db_models import (
            CanonicalConversationRollupJobModel,
        )

        jobs = int(
            await session.scalar(
                select(func.count(CanonicalConversationRollupJobModel.conversation_id))
            )
            or 0
        )
        job = await session.get(CanonicalConversationRollupJobModel, claim.conversation_id)
    assert jobs == 1
    assert job is not None
    assert job.status == "processing"
    assert claim.conversation_id == job.conversation_id


async def test_v2_health_snapshot_counts_canonical_not_legacy_scopes(
    database: Database,
) -> None:
    scope = await _prepare_v2_private(database)
    policy = _policy()
    uow = ScopedEventLedgerUnitOfWork(database, config=policy)
    repository = ConversationRollupRepository(database, policy)
    await uow.append(
        scope=scope,
        platform_message_id="v2-health-1",
        sender_user_id="1001",
        direction="inbound",
        content="lag-event",
    )
    health = await repository.health_snapshot()
    assert health["scope_count"] == 1
    assert health["max_lag_events"] == 1
    assert int(health["max_lag_characters"]) > 0
    assert health["recent_infrastructure_error_category"] is None
    assert "1001" not in repr(health)
    assert "8000" not in repr(health)


async def _append_v2(
    uow: ScopedEventLedgerUnitOfWork,
    scope: ConversationScope,
    count: int,
    *,
    start: int = 1,
    origin: str = "user_message",
) -> None:
    for index in range(start, start + count):
        await uow.append(
            scope=scope,
            platform_message_id=f"v2-message-{index}",
            sender_user_id="1001",
            direction="inbound",
            content=f"event-{index}",
            occurred_at=datetime(2026, 8, 20, 0, index % 60, tzinfo=UTC),
            origin=origin,
        )


async def test_v2_new_generation_replay_deletes_canonical_checkpoint(
    database: Database,
) -> None:
    from sqlalchemy import func, select

    from qq_ai_bot.conversation.canonical_db_models import (
        CanonicalConversationRollupJobModel,
        CanonicalConversationRollupModel,
    )

    scope = await _prepare_v2_private(database)
    policy = _policy()
    uow = ScopedEventLedgerUnitOfWork(database, config=policy)
    repository = ConversationRollupRepository(database, policy)
    await _append_v2(uow, scope, 4)
    claim = await repository.claim_next_job(lease_owner="worker", lease_seconds=30)
    assert claim is not None and claim.conversation_id
    candidate = await repository.candidate_for_claim(claim)
    assert candidate is not None
    await repository.commit_candidate(
        claim,
        candidate,
        summary_text="canonical-before-new",
        summary_kind=RollupKind.EXTRACTIVE,
    )
    inbound = InboundMessage(
        message_id="ai-new-1",
        event_type="message:test",
        scope_type=ScopeType.PRIVATE,
        sender=SenderIdentity(user_id="1001"),
        text="reset context",
        bot_user_id="8000",
    )
    first = await uow.append_new_generation_command(scope=scope, inbound=inbound)
    assert first.generation_changed is True
    assert first.scope.generation == 2
    replay = await uow.append_new_generation_command(scope=scope, inbound=inbound)
    assert replay.generation_changed is False
    assert replay.scope.generation == 2
    assert replay.event.id == first.event.id
    async with database.sessions() as session:
        rollups = int(
            await session.scalar(
                select(func.count(CanonicalConversationRollupModel.conversation_id))
            )
            or 0
        )
        jobs = int(
            await session.scalar(
                select(func.count(CanonicalConversationRollupJobModel.conversation_id))
            )
            or 0
        )
    assert rollups == 0
    assert jobs == 0
    snapshot = await repository.load_prompt_snapshot(scope)
    assert snapshot.scope.generation == 2
    assert snapshot.rollup is None


async def test_v2_visual_summary_and_append_use_prompt_accounting(
    database: Database,
) -> None:
    from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel
    from qq_ai_bot.conversation.rollup.prompt_accounting import (
        durable_uncovered_event_characters,
    )
    from qq_ai_bot.conversation.rollup.repository import recount_canonical_uncovered
    from qq_ai_bot.persistence.models import ChatEventModel

    scope = await _prepare_v2_private(database)
    policy = _policy()
    uow = ScopedEventLedgerUnitOfWork(database, config=policy)
    repository = ConversationRollupRepository(database, policy)
    appended = await uow.append(
        scope=scope,
        platform_message_id="v2-visual-1",
        sender_user_id="1001",
        direction="inbound",
        content="plain",
    )
    expected_append = durable_uncovered_event_characters(
        appended.event,
        bot_display_name=policy.bot_display_name,
        timezone=policy.timezone,
    )
    assert appended.scope.uncovered_character_count == expected_append
    assert appended.scope.uncovered_character_count != len("plain")
    await uow.set_visual_summary(appended.event.id, "a newly available visual description")
    state, _rollup, _job = await repository.status(scope)
    assert state is not None
    refreshed = await repository.load_prompt_snapshot(scope)
    expected_visual = durable_uncovered_event_characters(
        refreshed.raw_events[0],
        bot_display_name=policy.bot_display_name,
        timezone=policy.timezone,
    )
    assert state.uncovered_character_count == expected_visual
    async with database.immediate_session() as session:
        event = await session.get(ChatEventModel, appended.event.id)
        assert event is not None and event.canonical_conversation_id
        conversation = await session.get(
            CanonicalConversationModel, event.canonical_conversation_id
        )
        assert conversation is not None
        conversation.uncovered_character_count = 0
    await uow.set_visual_summary(appended.event.id, "x")
    state, _rollup, _job = await repository.status(scope)
    assert state is not None
    assert state.uncovered_character_count >= 0
    async with database.immediate_session() as session:
        event = await session.get(ChatEventModel, appended.event.id)
        assert event is not None and event.canonical_conversation_id
        conversation = await session.get(
            CanonicalConversationModel, event.canonical_conversation_id
        )
        assert conversation is not None
        recounted = await recount_canonical_uncovered(session, conversation, policy)
        assert recounted[0] == 1
        assert recounted[1] == state.uncovered_character_count


async def test_v2_canonical_queries_exclude_duplicate_events(database: Database) -> None:
    from uuid import uuid4

    from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel
    from qq_ai_bot.conversation.rollup.repository import recount_canonical_uncovered
    from qq_ai_bot.persistence.models import ChatEventModel

    scope = await _prepare_v2_private(database)
    policy = _policy()
    uow = ScopedEventLedgerUnitOfWork(database, config=policy)
    repository = ConversationRollupRepository(database, policy)
    keeper = await uow.append(
        scope=scope,
        platform_message_id="v2-keep",
        sender_user_id="1001",
        direction="inbound",
        content="visible-keeper",
    )
    async with database.immediate_session() as session:
        original = await session.get(ChatEventModel, keeper.event.id)
        assert original is not None
        session.add(
            ChatEventModel(
                bot_user_id=original.bot_user_id,
                platform_message_id="v2-dup",
                scope_type=original.scope_type,
                group_id=original.group_id,
                private_peer_user_id=original.private_peer_user_id,
                sender_user_id=original.sender_user_id,
                direction="inbound",
                event_kind="message",
                content="hidden-duplicate",
                visual_summary="",
                segments_json="[]",
                origin="user_message",
                occurred_at=original.occurred_at,
                observed_at=original.observed_at,
                canonical_event_id=str(uuid4()),
                canonical_conversation_id=original.canonical_conversation_id,
                author_kind=original.author_kind,
                author_person_id=original.author_person_id,
                author_presence_id=original.author_presence_id,
                ingress_presence_id=original.ingress_presence_id,
                utterance_fingerprint="f" * 64,
                suppression_status="duplicate",
            )
        )
        await session.flush()
        conversation = await session.get(
            CanonicalConversationModel, original.canonical_conversation_id
        )
        assert conversation is not None
        conversation.last_event_id = max(int(conversation.last_event_id), int(original.id) + 10)
    snapshot = await repository.load_prompt_snapshot(scope)
    assert [event.content for event in snapshot.raw_events] == ["visible-keeper"]
    async with database.immediate_session() as session:
        event = await session.get(ChatEventModel, keeper.event.id)
        assert event is not None
        conversation = await session.get(
            CanonicalConversationModel, event.canonical_conversation_id
        )
        assert conversation is not None
        recounted = await recount_canonical_uncovered(session, conversation, policy)
    assert recounted[0] == 1


async def _commit_overlay(
    repository: ConversationRollupRepository,
    service: ConversationRollupService,
    scope: ConversationScope,
    *,
    owner: str = "overlay",
) -> tuple[RollupJobClaim, RollupCommitResult]:
    claim = await repository.claim_scope_for_foreground(scope, lease_owner=owner, lease_seconds=30)
    assert claim is not None
    candidate = await repository.candidate_for_claim(claim, emergency=True)
    assert candidate is not None
    summary, kind = service.emergency(candidate)
    assert kind is RollupKind.EMERGENCY
    result = await repository.commit_emergency_overlay(claim, candidate, summary)
    return claim, result


async def test_v2_emergency_overlay_does_not_mutate_semantic_checkpoint(
    database: Database,
) -> None:
    from sqlalchemy import func, select

    from qq_ai_bot.conversation.canonical_db_models import (
        CanonicalConversationRollupEmergencyOverlayModel,
        CanonicalConversationRollupJobModel,
        CanonicalConversationRollupModel,
        ConversationLegacyAliasModel,
    )

    scope = await _prepare_v2_private(database)
    policy = _policy()
    uow = ScopedEventLedgerUnitOfWork(database, config=policy)
    repository = ConversationRollupRepository(database, policy)
    service = ConversationRollupService(models=None, config=policy, timeout_seconds=0.1)
    await _append_v2(uow, scope, 4)
    async with database.sessions() as session:
        alias = await session.scalar(
            select(ConversationLegacyAliasModel).where(
                ConversationLegacyAliasModel.scope_key == scope.key
            )
        )
        assert alias is not None
        conversation_id = alias.conversation_id
    from qq_ai_bot.conversation.canonical_rollup import drain_rollup_signals

    await drain_rollup_signals(database, policy)
    await _seed_job_last_error(database, conversation_id=conversation_id)
    claim, result = await _commit_overlay(repository, service, scope, owner="v2-overlay")
    assert claim.conversation_id
    snapshot = await repository.load_prompt_snapshot(scope)
    assert snapshot.rewrite_pending is True
    assert snapshot.overlay is not None
    assert snapshot.rollup is not None
    assert snapshot.rollup.summary_kind is RollupKind.EMERGENCY
    assert snapshot.effective_coverage == result.rollup.covered_through_event_id
    state, effective, job = await repository.status(scope)
    assert state is not None
    assert state.uncovered_event_count == 4
    assert effective is not None
    assert effective.summary_kind is RollupKind.EMERGENCY
    assert job is not None
    assert job["last_error_category"] is None
    async with database.sessions() as session:
        rollups = int(
            await session.scalar(
                select(func.count(CanonicalConversationRollupModel.conversation_id))
            )
            or 0
        )
        overlay = await session.get(
            CanonicalConversationRollupEmergencyOverlayModel, claim.conversation_id
        )
        stored_job = await session.get(CanonicalConversationRollupJobModel, claim.conversation_id)
        assert rollups == 0
        assert overlay is not None
        assert overlay.base_semantic_revision == 0
        assert overlay.covered_through_event_id == snapshot.effective_coverage
        assert stored_job is not None
        assert stored_job.status == "pending"
        assert stored_job.last_error_category is None
        next_at = stored_job.next_attempt_at
        if next_at.tzinfo is None:
            next_at = next_at.replace(tzinfo=UTC)
        assert next_at > datetime.now(UTC)


def _catchup_semantic_policy() -> RollupPolicyConfig:
    return RollupPolicyConfig(
        raw_tail_events=2,
        raw_tail_characters=100_000,
        trigger_events=2,
        trigger_characters=100_000,
        stop_events=1,
        stop_characters=0,
        batch_max_events=1,
        batch_max_characters=100_000,
        summary_max_characters=2_000,
    )


def _detailed_status_fixture(*, overlay: bool) -> ConversationRollupDetailedStatus:
    now = datetime(2026, 8, 25, tzinfo=UTC)
    scope = ConversationScopeState(
        id=1,
        scope=ConversationScope.private("bot", "peer"),
        generation=1,
        starts_after_event_id=0,
        last_event_id=10,
        last_generation_change_event_id=0,
        uncovered_event_count=8,
        uncovered_character_count=400,
        created_at=now,
        updated_at=now,
    )
    overlay_status = (
        RollupCheckpointStatus(
            kind=RollupKind.EMERGENCY,
            revision=1,
            covered_through_event_id=6,
        )
        if overlay
        else None
    )
    return ConversationRollupDetailedStatus(
        scope=scope,
        semantic=RollupCheckpointStatus(
            kind=RollupKind.MODEL,
            revision=2,
            covered_through_event_id=2,
        ),
        overlay=overlay_status,
        effective_coverage=6 if overlay else 2,
        rewrite_pending=overlay,
        semantic_uncovered_event_count=8,
        semantic_uncovered_character_count=400,
        effective_prompt_tail_event_count=2 if overlay else 8,
        effective_prompt_tail_character_count=80 if overlay else 400,
        job=None,
    )


class _CountingSuccessModels:
    def __init__(self) -> None:
        self.calls = 0

    async def execute(self, *_args: object, **_kwargs: object) -> ChatResponse:
        self.calls += 1
        return ChatResponse(content="semantic catch-up summary", latency_seconds=0)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


async def _assert_policy_park_then_mixed_catchup(database: Database, *, v2: bool) -> None:
    policy = _policy()
    if v2:
        scope = await _prepare_v2_private(database, peer="1001")
        uow = ScopedEventLedgerUnitOfWork(database, config=policy)
        await _append_v2(uow, scope, 4, origin="plugin_background")
    else:
        scope = ConversationScope.private("bot-a", "peer-policy-park")
        uow = ScopedEventLedgerUnitOfWork(database, config=policy)
        await _append(uow, scope, 4, origin="plugin_background")
    repository = ConversationRollupRepository(database, policy)
    models = _CountingSuccessModels()
    service = ConversationRollupService(models=models, config=policy, timeout_seconds=1)
    before = await repository.load_prompt_snapshot(scope)
    assert before.overlay is None
    worker = _background_worker(repository, service)
    await _run_worker_until(worker, lambda: _overlay_ready(repository, scope))
    snapshot = await repository.load_prompt_snapshot(scope)
    assert models.calls == 0
    assert snapshot.rewrite_pending is True
    assert snapshot.overlay is not None
    assert snapshot.overlay.summary_kind is RollupKind.EMERGENCY
    assert snapshot.scope.uncovered_event_count == 4
    detailed = await repository.detailed_status(scope)
    assert detailed.rewrite_pending is True
    assert detailed.semantic is None
    assert detailed.job is not None
    assert detailed.job.status == "pending"
    assert detailed.job.last_error_category == LLM_ORIGIN_INELIGIBLE
    parked_created_at = detailed.job.created_at
    parked_signal = detailed.job.signal_revision
    health = await repository.health_snapshot()
    assert health["last_extractive_at"] is None
    marked = datetime.now(UTC)
    async with database.sessions() as session:
        if v2:
            from sqlalchemy import select

            from qq_ai_bot.conversation.canonical_db_models import (
                CanonicalConversationRollupJobModel,
                CanonicalConversationRollupModel,
                ConversationLegacyAliasModel,
            )

            alias = await session.scalar(
                select(ConversationLegacyAliasModel).where(
                    ConversationLegacyAliasModel.scope_key == scope.key
                )
            )
            assert alias is not None
            assert (
                await session.get(CanonicalConversationRollupModel, alias.conversation_id) is None
            )
            stored = await session.get(CanonicalConversationRollupJobModel, alias.conversation_id)
        else:
            assert await session.get(ConversationRollupModel, snapshot.scope.id) is None
            stored = await session.get(ConversationRollupJobModel, snapshot.scope.id)
        assert stored is not None
        assert stored.status == "pending"
        assert stored.last_error_category == LLM_ORIGIN_INELIGIBLE
        park_delay = _aware(stored.next_attempt_at) - marked
        assert park_delay >= POLICY_PARK_DELAY - timedelta(seconds=5)
        assert park_delay <= POLICY_PARK_DELAY + timedelta(seconds=5)
    assert await repository.claim_next_job(lease_owner="immediate", lease_seconds=30) is None
    assert await repository.claim_next_job(lease_owner="config-only", lease_seconds=30) is None
    if v2:
        await _append_v2(uow, scope, 3, start=5, origin="user_message")
    else:
        await _append(uow, scope, 3, start=5, origin="user_message")
    _state, _effective, woken = await repository.status(scope)
    assert woken is not None
    assert woken["status"] == "pending"
    assert woken["last_error_category"] == LLM_ORIGIN_INELIGIBLE
    assert woken["created_at"] == parked_created_at
    assert int(woken["signal_revision"]) > int(parked_signal)
    woken_claim = await repository.claim_next_job(lease_owner="after-ledger", lease_seconds=30)
    assert woken_claim is not None
    await repository.release_owner("after-ledger")
    catchup = _background_worker(repository, service)
    await _run_worker_until(catchup, lambda: _overlay_cleared(repository, scope))
    assert models.calls >= 1
    caught = await repository.load_prompt_snapshot(scope)
    assert caught.overlay is None
    assert caught.rewrite_pending is False
    assert caught.rollup is not None
    assert caught.rollup.summary_kind is RollupKind.MODEL
    assert caught.rollup.summary_text == "semantic catch-up summary"
    assert caught.effective_coverage == caught.rollup.covered_through_event_id


async def _assert_policy_signal_kept(database: Database, *, v2: bool) -> None:
    policy = _policy()
    if v2:
        scope = await _prepare_v2_private(database, bot="8012", peer="1001")
        uow = ScopedEventLedgerUnitOfWork(database, config=policy)
        await _append_v2(uow, scope, 4, origin="plugin_background")
    else:
        scope = ConversationScope.private("bot-a", "peer-policy-signal")
        uow = ScopedEventLedgerUnitOfWork(database, config=policy)
        await _append(uow, scope, 4, origin="plugin_background")
    repository = ConversationRollupRepository(database, policy)
    service = ConversationRollupService(models=None, config=policy, timeout_seconds=0.1)
    claim = await repository.claim_next_job(lease_owner="policy-signal", lease_seconds=30)
    assert claim is not None
    candidate = await repository.candidate_for_claim(claim)
    assert candidate is not None
    assert service.candidate_uses_model(candidate) is False
    if v2:
        await _append_v2(uow, scope, 1, start=5, origin="plugin_background")
    else:
        await _append(uow, scope, 1, start=5, origin="plugin_background")
    summary, kind = service.emergency(candidate)
    assert kind is RollupKind.EMERGENCY
    await repository.commit_emergency_overlay(
        claim,
        candidate,
        summary,
        error_category=LLM_ORIGIN_INELIGIBLE,
        disposition=EmergencyOverlayDisposition.POLICY,
        source_emergency=False,
    )
    _state, _effective, job = await repository.status(scope)
    assert job is not None
    assert job["status"] == "pending"
    assert job["last_error_category"] == LLM_ORIGIN_INELIGIBLE
    detailed = await repository.detailed_status(scope)
    assert detailed.rewrite_pending is True
    assert detailed.job is not None
    assert detailed.job.last_error_category == LLM_ORIGIN_INELIGIBLE
    woken = await repository.claim_next_job(lease_owner="after-signal", lease_seconds=30)
    assert woken is not None


async def _assert_model_failure_backoff(database: Database, *, v2: bool) -> None:
    policy = _policy()
    if v2:
        scope = await _prepare_v2_private(database, bot="8013", peer="1001")
        uow = ScopedEventLedgerUnitOfWork(database, config=policy)
        await _append_v2(uow, scope, 4)
    else:
        scope = ConversationScope.private("bot-a", "peer-backoff")
        uow = ScopedEventLedgerUnitOfWork(database, config=policy)
        await _append(uow, scope, 4)
    repository = ConversationRollupRepository(database, policy)
    failing = ConversationRollupService(
        models=_QualityFailModels(),
        config=policy,
        timeout_seconds=1,
    )
    first = _background_worker(repository, failing, retry_max_seconds=20)
    marked = datetime.now(UTC)
    await _run_worker_until(first, lambda: _overlay_ready(repository, scope))
    _state, _effective, job = await repository.status(scope)
    assert job is not None
    assert job["failure_count"] == 1
    assert job["last_error_category"] == "model_quality"
    snapshot = await repository.load_prompt_snapshot(scope)
    assert snapshot.rewrite_pending is True
    assert snapshot.scope.uncovered_event_count == 4
    async with database.sessions() as session:
        if v2:
            from sqlalchemy import select

            from qq_ai_bot.conversation.canonical_db_models import (
                CanonicalConversationRollupJobModel,
                CanonicalConversationRollupModel,
                ConversationLegacyAliasModel,
            )

            alias = await session.scalar(
                select(ConversationLegacyAliasModel).where(
                    ConversationLegacyAliasModel.scope_key == scope.key
                )
            )
            assert alias is not None
            stored = await session.get(CanonicalConversationRollupJobModel, alias.conversation_id)
            semantic = await session.get(CanonicalConversationRollupModel, alias.conversation_id)
            conversation_id = alias.conversation_id
            scope_id = None
        else:
            stored = await session.get(ConversationRollupJobModel, snapshot.scope.id)
            semantic = await session.get(ConversationRollupModel, snapshot.scope.id)
            conversation_id = None
            scope_id = snapshot.scope.id
        assert semantic is None
        assert stored is not None
        first_delay = (_aware(stored.next_attempt_at) - marked).total_seconds()
        assert 10 <= first_delay <= 25
    await _force_rollup_job_due(database, scope_id=scope_id, conversation_id=conversation_id)
    marked = datetime.now(UTC)
    second = _background_worker(repository, failing, retry_max_seconds=20)
    await _run_worker_until(second, lambda: _overlay_ready(repository, scope))
    _state, _effective, job = await repository.status(scope)
    assert job is not None
    assert job["failure_count"] == 2
    async with database.sessions() as session:
        if v2:
            stored = await session.get(CanonicalConversationRollupJobModel, conversation_id)
        else:
            stored = await session.get(ConversationRollupJobModel, scope_id)
        assert stored is not None
        second_delay = (_aware(stored.next_attempt_at) - marked).total_seconds()
        assert 15 <= second_delay <= 25
        assert stored.failure_count == 2
    await _force_rollup_job_due(database, scope_id=scope_id, conversation_id=conversation_id)
    success = ConversationRollupService(
        models=_SuccessModels(),
        config=policy,
        timeout_seconds=1,
    )
    catchup = _background_worker(repository, success, retry_max_seconds=20)
    await _run_worker_until(catchup, lambda: _overlay_cleared(repository, scope))
    caught = await repository.load_prompt_snapshot(scope)
    assert caught.overlay is None
    assert caught.rollup is not None
    assert caught.rollup.summary_kind is RollupKind.MODEL
    assert caught.rollup.summary_text == "semantic catch-up summary"


async def _assert_model_failure_keeps_backoff_when_signal_arrives(
    database: Database, *, v2: bool
) -> None:
    policy = _policy()
    if v2:
        scope = await _prepare_v2_private(database, bot="8015", peer="1001")
        uow = ScopedEventLedgerUnitOfWork(database, config=policy)
        await _append_v2(uow, scope, 4)
    else:
        scope = ConversationScope.private("bot-a", "peer-backoff-signal")
        uow = ScopedEventLedgerUnitOfWork(database, config=policy)
        await _append(uow, scope, 4)
    repository = ConversationRollupRepository(database, policy)
    service = ConversationRollupService(models=None, config=policy, timeout_seconds=0.1)
    claim = await repository.claim_next_job(lease_owner="model-fail-signal", lease_seconds=30)
    assert claim is not None
    claimed_signal = claim.claimed_signal_revision
    candidate = await repository.candidate_for_claim(claim)
    assert candidate is not None
    if v2:
        await _append_v2(uow, scope, 1, start=5)
    else:
        await _append(uow, scope, 1, start=5)
    summary, kind = service.emergency(candidate)
    assert kind is RollupKind.EMERGENCY
    marked = datetime.now(UTC)
    await repository.commit_emergency_overlay(
        claim,
        candidate,
        summary,
        error_category="model_quality",
        disposition=EmergencyOverlayDisposition.MODEL_FAILURE,
        source_emergency=False,
        retry_max_seconds=20,
    )
    _state, _effective, job = await repository.status(scope)
    assert job is not None
    assert job["status"] == "pending"
    assert job["failure_count"] == 1
    assert job["last_error_category"] == "model_quality"
    assert int(job["signal_revision"]) == claimed_signal + 1
    snapshot = await repository.load_prompt_snapshot(scope)
    assert snapshot.rewrite_pending is True
    assert snapshot.overlay is not None
    assert snapshot.overlay.summary_kind is RollupKind.EMERGENCY
    assert snapshot.scope.uncovered_event_count == 5
    detailed = await repository.detailed_status(scope)
    assert detailed.rewrite_pending is True
    assert detailed.semantic is None
    assert detailed.job is not None
    assert detailed.job.signal_revision == claimed_signal + 1
    async with database.sessions() as session:
        if v2:
            from sqlalchemy import select

            from qq_ai_bot.conversation.canonical_db_models import (
                CanonicalConversationRollupJobModel,
                CanonicalConversationRollupModel,
                ConversationLegacyAliasModel,
            )

            alias = await session.scalar(
                select(ConversationLegacyAliasModel).where(
                    ConversationLegacyAliasModel.scope_key == scope.key
                )
            )
            assert alias is not None
            stored = await session.get(CanonicalConversationRollupJobModel, alias.conversation_id)
            semantic_row = await session.get(
                CanonicalConversationRollupModel, alias.conversation_id
            )
        else:
            stored = await session.get(ConversationRollupJobModel, snapshot.scope.id)
            semantic_row = await session.get(ConversationRollupModel, snapshot.scope.id)
        assert semantic_row is None
        assert stored is not None
        assert stored.status == "pending"
        assert stored.signal_revision == claimed_signal + 1
        delay = (_aware(stored.next_attempt_at) - marked).total_seconds()
        assert 10 <= delay <= 25
    assert await repository.claim_next_job(lease_owner="immediate", lease_seconds=30) is None


async def _assert_stale_reset_rejects(database: Database, *, v2: bool) -> None:
    policy = _policy()
    if v2:
        scope = await _prepare_v2_private(database, bot="8014", peer="1001")
        uow = ScopedEventLedgerUnitOfWork(database, config=policy)
        await _append_v2(uow, scope, 4)
        inbound = InboundMessage(
            message_id="ai-new-stale-v2",
            event_type="message:test",
            scope_type=ScopeType.PRIVATE,
            sender=SenderIdentity(user_id="1001"),
            text="reset context",
            bot_user_id="8014",
        )
    else:
        scope = ConversationScope.private("bot-a", "peer-stale-reset")
        uow = ScopedEventLedgerUnitOfWork(database, config=policy)
        await _append(uow, scope, 4)
        inbound = InboundMessage(
            message_id="ai-new-stale",
            event_type="message:test",
            scope_type=ScopeType.PRIVATE,
            sender=SenderIdentity(user_id="peer-stale-reset"),
            text="reset context",
            bot_user_id="bot-a",
        )
    repository = ConversationRollupRepository(database, policy)
    service = ConversationRollupService(models=None, config=policy, timeout_seconds=0.1)
    claim = await repository.claim_next_job(lease_owner="stale", lease_seconds=30)
    assert claim is not None
    candidate = await repository.candidate_for_claim(claim)
    assert candidate is not None
    summary, _kind = service.emergency(candidate)
    changed = await uow.append_new_generation_command(scope=scope, inbound=inbound)
    assert changed.generation_changed is True
    with pytest.raises((RollupLeaseLostError, RollupSourceChangedError)):
        await repository.commit_emergency_overlay(
            claim,
            candidate,
            summary,
            error_category=LLM_ORIGIN_INELIGIBLE,
            disposition=EmergencyOverlayDisposition.POLICY,
            source_emergency=False,
        )
    with pytest.raises((RollupLeaseLostError, RollupSourceChangedError)):
        await repository.commit_candidate(
            claim,
            candidate,
            summary_text="stale semantic",
            summary_kind=RollupKind.MODEL,
        )
    snapshot = await repository.load_prompt_snapshot(scope)
    assert snapshot.overlay is None
    assert snapshot.rewrite_pending is False
    assert snapshot.scope.generation == 2
    async with database.sessions() as session:
        if v2:
            from sqlalchemy import func, select

            from qq_ai_bot.conversation.canonical_db_models import (
                CanonicalConversationRollupEmergencyOverlayModel,
                CanonicalConversationRollupModel,
            )

            assert (
                int(
                    await session.scalar(
                        select(func.count(CanonicalConversationRollupModel.conversation_id))
                    )
                    or 0
                )
                == 0
            )
            assert (
                int(
                    await session.scalar(
                        select(
                            func.count(
                                CanonicalConversationRollupEmergencyOverlayModel.conversation_id
                            )
                        )
                    )
                    or 0
                )
                == 0
            )
        else:
            assert await session.get(ConversationRollupModel, snapshot.scope.id) is None
            overlay_row = await session.get(
                ConversationRollupEmergencyOverlayModel, snapshot.scope.id
            )
            assert overlay_row is None


async def test_v2_stale_result_after_generation_reset_is_rejected(database: Database) -> None:
    await _assert_stale_reset_rejects(database, v2=True)
