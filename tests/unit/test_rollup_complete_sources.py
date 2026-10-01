"""Compaction preserves the data it claims to have read, including source identity."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from qq_ai_bot.conversation.rollup.models import RollupCandidate, RollupPolicyConfig
from qq_ai_bot.conversation.rollup.renderer import (
    render_rollup_message,
    rollup_source_projection,
    serialize_compaction_source_events,
)
from qq_ai_bot.conversation.rollup.service import ConversationRollupService
from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.domain.messages import ChatResponse
from qq_ai_bot.persistence.repository_records import EventRecord


def event() -> EventRecord:
    return EventRecord(
        id=42,
        bot_user_id="9000",
        platform_message_id="transport-only",
        scope_type=ScopeType.GROUP,
        sender_user_id="1001",
        direction="inbound",
        content="",
        visual_summary="",
        segments=({"type": "at", "data": {"qq": "9000"}},),
        occurred_at=datetime(2026, 10, 1, tzinfo=UTC),
        sender_group_card="same-name",
        group_id="group",
        mentioned_user_ids=("9000",),
        reply_to_event_id=12,
        author_kind="person",
        author_person_id="person-one",
    )


def test_source_preserves_internal_identity_reply_and_segment_only_mentions() -> None:
    source = rollup_source_projection(event())
    assert '"event_id":42' in source
    assert '"author_person_id":"person-one"' in source
    assert '"direction":"inbound"' in source
    assert '"reply_to_event_id":12' in source
    assert "提及:" in source
    assert "transport-only" not in source
    assert source != rollup_source_projection(replace(event(), author_person_id="person-two"))


def test_oversized_source_cannot_be_silently_truncated() -> None:
    from qq_ai_bot.conversation.rollup.prompt_accounting import source_accounting_characters
    from qq_ai_bot.conversation.rollup.repository import take_batch

    source_event = replace(event(), content="先不要发布。" * 30 + "必须等用户批准。", segments=())
    batch = take_batch((source_event,), RollupPolicyConfig(batch_max_characters=80))
    source = serialize_compaction_source_events(batch)
    assert batch == (source_event,)
    assert source.endswith("必须等用户批准。")
    assert source_accounting_characters(batch) == len(source) > 80


class RecordingModel:
    def __init__(self, *, fail_at: int | None = None) -> None:
        self.sources: list[str] = []
        self.fail_at = fail_at
        self.output_budgets: list[int | None] = []

    async def execute(self, _task, request, *, priority=None):
        del priority
        self.output_budgets.append(request.max_output_tokens)
        body = request.messages[-1].content
        self.sources.append(
            body.split("New source events:\n", 1)[1].rsplit("\n\nCharacter limit:", 1)[0]
        )
        if self.fail_at == len(self.sources):
            raise RuntimeError("model disconnected")
        return ChatResponse(content="Continuity: #42; pending approval.", latency_seconds=0)


def candidate() -> RollupCandidate:
    events = (replace(event(), content="new-source " * 100 + "LAST_CONSTRAINT", segments=()),)
    return RollupCandidate(1, 1, 0, 0, "", events, 1, 100, "source-fingerprint")


@pytest.mark.asyncio
async def test_model_reads_every_source_chunk_before_returning_semantic_result() -> None:
    model = RecordingModel()
    policy = RollupPolicyConfig(batch_max_characters=256)
    service = ConversationRollupService(models=model, config=policy, timeout_seconds=2)
    text, kind = await service.summarize_candidate(candidate())
    assert kind.value == "model"
    assert text.startswith("Continuity")
    assert "".join(model.sources) == serialize_compaction_source_events(candidate().events)
    assert len(model.sources) > 1
    assert all(len(source) <= 256 for source in model.sources)


@pytest.mark.asyncio
async def test_failed_source_chunk_cannot_return_semantic_success() -> None:
    model = RecordingModel(fail_at=2)
    service = ConversationRollupService(
        models=model, config=RollupPolicyConfig(batch_max_characters=256), timeout_seconds=2
    )
    with pytest.raises(RuntimeError, match="disconnected"):
        await service.summarize_candidate(candidate())
    assert service.metrics.model_summaries == 0


@pytest.mark.asyncio
async def test_source_chunks_fit_actual_compaction_profile_input_budget() -> None:
    from qq_ai_bot.model_runtime.capacity import ModelCapacity, estimate_request_tokens

    class SmallInputModel(RecordingModel):
        def capacity(self, _task):
            return ModelCapacity(input_tokens=3000)

        async def execute(self, task, request, *, priority=None):
            assert estimate_request_tokens(request) <= 3000
            return await super().execute(task, request, priority=priority)

    model = SmallInputModel()
    service = ConversationRollupService(
        models=model, config=RollupPolicyConfig(batch_max_characters=32_768), timeout_seconds=2
    )
    await service.summarize_candidate(candidate())
    assert len(model.sources) > 1
    assert "".join(model.sources) == serialize_compaction_source_events(candidate().events)


def test_emergency_view_discloses_missing_history_and_internal_source_range() -> None:
    message = render_rollup_message("surviving tail", kind="emergency", covered_through_event_id=42)
    assert "Incomplete emergency" in message.content
    assert "not a complete semantic summary" in message.content
    assert "event_id=42" in message.content
    assert message.role == "user"


@pytest.mark.asyncio
async def test_candidate_carries_its_hot_output_policy() -> None:
    model = RecordingModel()
    service = ConversationRollupService(
        models=model, config=RollupPolicyConfig(), timeout_seconds=2
    )
    policy = RollupPolicyConfig(max_output_tokens=1234, batch_max_characters=256)
    await service.summarize_candidate(replace(candidate(), policy=policy))
    assert model.output_budgets and set(model.output_budgets) == {1234}


async def _seed_private(database, *, peer: str, count: int, policy: RollupPolicyConfig):
    from qq_ai_bot.domain.conversations import ConversationScope
    from qq_ai_bot.identity.canonical_repository import ensure_person, ensure_presence
    from qq_ai_bot.persistence.scoped_event_uow import ScopedEventLedgerUnitOfWork

    async with database.sessions() as session, session.begin():
        await ensure_presence(session, "8000")
        await ensure_person(session, peer)
    scope = ConversationScope.private("8000", peer)
    writer = ScopedEventLedgerUnitOfWork(database, config=policy)
    for index in range(count):
        await writer.append(
            scope=scope,
            platform_message_id=f"{peer}-{index}",
            sender_user_id=peer,
            direction="inbound",
            content="tiny fact",
            occurred_at=datetime(2026, 10, 1, tzinfo=UTC),
        )
    return scope


@pytest.mark.asyncio
async def test_activity_window_keeps_more_than_512_small_events_without_compaction(
    database,
) -> None:
    from qq_ai_bot.conversation.rollup.repository import ConversationRollupRepository

    policy = RollupPolicyConfig()
    scope = await _seed_private(database, peer="1010", count=520, policy=policy)
    repository = ConversationRollupRepository(database, policy)
    snapshot = await repository.load_prompt_snapshot(scope)
    assert snapshot.raw_complete
    assert len(snapshot.raw_events) == 520
    assert await repository.claim_next_job(lease_owner="capacity", lease_seconds=30) is None


@pytest.mark.asyncio
async def test_hot_scope_policy_isolated_and_incomplete_raw_prefix_is_explicit(database) -> None:
    from qq_ai_bot.conversation.rollup.repository import ConversationRollupRepository

    base = RollupPolicyConfig(context_token_budget=10_000)
    first = await _seed_private(database, peer="1011", count=5, policy=base)
    second = await _seed_private(database, peer="1012", count=5, policy=base)
    budgets = {"1011": 60, "1012": 10_000}

    async def policy_for_scope(scope):
        await asyncio.sleep(0)
        return replace(base, context_token_budget=budgets[scope.private_peer_user_id])

    repository = ConversationRollupRepository(database, base, policy_for_scope=policy_for_scope)
    a, b = await asyncio.gather(
        repository.load_prompt_snapshot(first), repository.load_prompt_snapshot(second)
    )
    assert not a.raw_complete and len(a.raw_events) < 5
    assert b.raw_complete and len(b.raw_events) == 5
    budgets["1011"] = 10_000
    reread = await repository.load_prompt_snapshot(first)
    assert reread.raw_complete and len(reread.raw_events) == 5
    assert repository.config == base


@pytest.mark.asyncio
async def test_durable_prerequisite_keeps_actual_request_budget_until_deadline(database) -> None:
    import json
    import time

    from sqlalchemy import update

    from qq_ai_bot.conversation.rollup.repository import ConversationRollupRepository
    from qq_ai_bot.runtime.work_repository import WorkRepository
    from qq_ai_bot.runtime.work_schema_v1 import work

    policy = RollupPolicyConfig(context_token_budget=10_000)
    scope = await _seed_private(database, peer="1013", count=5, policy=policy)
    repository = ConversationRollupRepository(database, policy)
    snapshot = await repository.load_prompt_snapshot(scope)
    owner = WorkRepository(database)
    lease = await owner.acquire(snapshot.conversation_id, 1)
    item = await owner.accept(
        lease, source_key="capacity-prerequisite", source={}, goal="keep task"
    )
    try:
        checkpoint = {
            "context_rollup": {"coverage": 0, "token_budget": 60, "deadline": time.time() + 90}
        }
        async with database.immediate_session() as session:
            await session.execute(
                update(work)
                .where(work.c.id == item["id"])
                .values(checkpoint_json=json.dumps(checkpoint))
            )
        claim = await repository.claim_scope_for_foreground(
            scope, lease_owner="fit", lease_seconds=30
        )
        assert claim is not None
        candidate = await repository.candidate_for_claim(claim)
        assert candidate is not None and candidate.policy.context_token_budget == 60
        checkpoint["context_rollup"]["deadline"] = time.time() - 1
        async with database.immediate_session() as session:
            await session.execute(
                update(work)
                .where(work.c.id == item["id"])
                .values(checkpoint_json=json.dumps(checkpoint))
            )
        assert await repository.candidate_for_claim(claim) is None
    finally:
        await owner.release(lease)


@pytest.mark.asyncio
async def test_plugin_capacity_reads_hot_snapshot_and_shared_fixed_contract_reserve() -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    from tests.conftest import make_settings

    from qq_ai_bot.conversation.rollup.errors import ConversationCoverageError
    from qq_ai_bot.domain.conversations import ConversationScope
    from qq_ai_bot.domain.messages import InboundMessage, SenderIdentity
    from qq_ai_bot.persistence.event_repository import ConversationReadVersion
    from qq_ai_bot.services.context_assembler import ContextAssembler
    from qq_ai_bot.time.models import TimeContext

    scope = ConversationScope.private("8000", "1001")
    version = ConversationReadVersion(scope, "canonical", 1, 0, 0, (), (0, 0))
    ledger = MagicMock()
    ledger.read_scope_context = AsyncMock(return_value=(version, ()))
    assembler = ContextAssembler(
        settings=make_settings("sqlite+aiosqlite:///:memory:"),
        ledger=ledger,
        people=MagicMock(),
        memory_context=MagicMock(),
        relationships=MagicMock(),
        time_service=MagicMock(),
        rollup_repository=MagicMock(),
        rollup_service=MagicMock(),
        history_budget=lambda runtime: runtime.context.window_tokens - 2048,
    )
    runtime = SimpleNamespace(context=SimpleNamespace(window_tokens=8192))
    arguments = dict(
        inbound=InboundMessage(
            "one", "message", ScopeType.PRIVATE, SenderIdentity("1001"), "", bot_user_id="8000"
        ),
        content="x" * 25_000,
        metadata={},
        current_time=TimeContext(
            utc=datetime(2026, 10, 1, tzinfo=UTC),
            local=datetime(2026, 10, 1, tzinfo=UTC),
            timezone="UTC",
        ),
        read_history=False,
        projection_scope="plugin",
        runtime=runtime,
    )
    with pytest.raises(ConversationCoverageError, match="explicit compaction"):
        await assembler.assemble_plugin(**arguments)
    runtime.context.window_tokens = 16_384
    assert (await assembler.assemble_plugin(**arguments)).current_message.content == "x" * 25_000
