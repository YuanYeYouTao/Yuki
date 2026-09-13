"""C1 rollup rulers: protected tail, batch source cost, append, fit, and recount."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from tests.conftest import make_settings

from qq_ai_bot.conversation.canonical_db_models import (
    CanonicalConversationModel,
    CanonicalConversationRollupJobModel,
)
from qq_ai_bot.conversation.offline_recount import recount_all_canonical_uncovered
from qq_ai_bot.conversation.rollup.models import RollupCandidate, RollupKind, RollupPolicyConfig
from qq_ai_bot.conversation.rollup.prompt_accounting import (
    durable_uncovered_characters,
    durable_uncovered_event_characters,
    is_prompt_visible_message,
    prompt_accounting_characters,
    prompt_accounting_event_characters,
    prompt_visible_event_count,
    source_accounting_characters,
)
from qq_ai_bot.conversation.rollup.renderer import (
    bound_compaction_source_events,
    projection_hash,
    rollup_source_projection,
    serialize_compaction_source_events,
)
from qq_ai_bot.conversation.rollup.repository import (
    ConversationRollupRepository,
    eligible_prefix,
    protected_tail_start,
    recount_canonical_uncovered,
    take_batch,
)
from qq_ai_bot.conversation.rollup.service import ConversationRollupService
from qq_ai_bot.conversation.scope import ConversationTurnSnapshot
from qq_ai_bot.domain.conversations import ConversationScope, ScopeType
from qq_ai_bot.domain.messages import ChatResponse, InboundMessage, SenderIdentity
from qq_ai_bot.domain.profiles import UserProfileSnapshot
from qq_ai_bot.memory.enums import MemoryRetrievalMode
from qq_ai_bot.memory.models import MemoryRetrievalResult
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.repositories import EventLedgerRepository
from qq_ai_bot.persistence.repository_records import EventRecord
from qq_ai_bot.persistence.scoped_event_uow import ScopedEventLedgerUnitOfWork
from qq_ai_bot.services.context_assembler import ContextAssembler, _UncoveredPromptView
from qq_ai_bot.time.models import TimeContext

_NOW = datetime(2026, 8, 26, tzinfo=UTC)


def _policy(**overrides: object) -> RollupPolicyConfig:
    values: dict[str, object] = {
        "raw_tail_events": 2,
        "raw_tail_characters": 100_000,
        "trigger_events": 4,
        "trigger_characters": 100_000,
        "stop_events": 0,
        "stop_characters": 0,
        "batch_max_events": 8,
        "batch_max_characters": 100_000,
        "summary_max_characters": 2_000,
    }
    values.update(overrides)
    return RollupPolicyConfig(**values)  # type: ignore[arg-type]


def _durable_kwargs(policy: RollupPolicyConfig) -> dict[str, str]:
    return {
        "bot_display_name": policy.bot_display_name,
        "timezone": policy.timezone,
    }


def _message(event_id: int, content: str = "hello") -> EventRecord:
    return EventRecord(
        id=event_id,
        bot_user_id="8000",
        platform_message_id=f"msg-{event_id}",
        scope_type=ScopeType.PRIVATE,
        sender_user_id="1001",
        direction="inbound",
        content=content,
        visual_summary="",
        segments=(),
        occurred_at=_NOW + timedelta(seconds=event_id),
        event_kind="message",
        private_peer_user_id="1001",
    )


def _external(event_id: int, summary: str = "notice") -> EventRecord:
    return EventRecord(
        id=event_id,
        bot_user_id="8000",
        platform_message_id=f"ext-{event_id}",
        scope_type=ScopeType.PRIVATE,
        sender_user_id="8000",
        direction="external",
        content=summary,
        visual_summary="",
        segments=(),
        occurred_at=_NOW + timedelta(seconds=event_id),
        event_kind="external_event",
        origin="plugin_background",
        author_kind="system",
        source_plugin_id="github-monitor",
        external_source="github",
        external_event_key=f"key-{event_id}",
        external_event_type="PushEvent",
        external_payload={"body": "raw"},
        private_peer_user_id="1001",
    )


async def _prepare_private(
    database: Database, *, bot: str = "8000", peer: str = "1001"
) -> ConversationScope:
    from qq_ai_bot.identity.canonical_repository import ensure_person, ensure_presence

    async with database.sessions() as session, session.begin():
        await ensure_presence(session, bot)
        await ensure_person(session, peer, now=_NOW)
    return ConversationScope.private(bot, peer)


def test_prompt_visible_helper_excludes_external_from_count_and_characters() -> None:
    events = (_message(1), _external(2, "storm"), _message(3), _external(4, "more"))
    assert tuple(is_prompt_visible_message(event) for event in events) == (True, False, True, False)
    assert prompt_visible_event_count(events) == 2
    assert prompt_accounting_characters(events) == prompt_accounting_characters(
        (_message(1), _message(3))
    )
    assert prompt_accounting_event_characters(_external(2)) == 0
    assert source_accounting_characters(events) > prompt_accounting_characters(events)
    assert "raw" not in rollup_source_projection(_external(2, "notice"))


def test_protected_tail_counts_visible_messages_and_rides_interleaved_externals() -> None:
    policy = _policy(raw_tail_events=2, raw_tail_characters=100_000)
    events = (
        _external(1, "storm-1"),
        _external(2, "storm-2"),
        _external(3, "storm-3"),
        _message(4, "keep-a"),
        _external(5, "between"),
        _message(6, "keep-b"),
        _external(7, "after"),
    )
    start = protected_tail_start(events, policy)
    assert events[start].id == 4
    protected = events[start:]
    assert tuple(event.id for event in protected) == (4, 5, 6, 7)
    eligible = eligible_prefix(events, policy)
    assert tuple(event.id for event in eligible) == (1, 2, 3)
    assert prompt_visible_event_count(protected) == 2
    assert all(event.event_kind == "external_event" for event in eligible)


def test_long_messages_with_interleaved_externals_keep_n_visible_protected() -> None:
    policy = _policy(raw_tail_events=3, raw_tail_characters=50)
    events = (
        _message(1, "z" * 500),
        _external(2, "storm-old"),
        _message(3, "z" * 500),
        _external(4, "between-a"),
        _message(5, "z" * 500),
        _external(6, "between-b"),
        _message(7, "z" * 500),
        _external(8, "after"),
    )
    start = protected_tail_start(events, policy)
    protected = events[start:]
    visible = tuple(event for event in protected if event.event_kind == "message")
    assert prompt_visible_event_count(protected) == policy.raw_tail_events
    assert tuple(event.id for event in visible) == (3, 5, 7)
    assert tuple(event.id for event in protected) == (3, 4, 5, 6, 7, 8)
    assert 1 not in {event.id for event in protected}
    eligible = eligible_prefix(events, policy)
    assert tuple(event.id for event in eligible) == (1, 2)


def test_external_only_prefix_is_fully_eligible() -> None:
    policy = _policy(raw_tail_events=2)
    events = tuple(_external(index) for index in range(1, 6))
    assert protected_tail_start(events, policy) == len(events)
    assert eligible_prefix(events, policy) == events


def test_take_batch_serialized_source_respects_cap_and_separators() -> None:
    policy = _policy(batch_max_events=8, batch_max_characters=400)
    events = tuple(_external(index, "z" * 40) for index in range(1, 6))
    batch = take_batch(events, policy)
    assert batch
    serialized = bound_compaction_source_events(
        batch,
        timezone=policy.timezone,
        max_characters=policy.batch_max_characters,
    )
    unbounded = serialize_compaction_source_events(batch, timezone=policy.timezone)
    assert serialized == unbounded
    assert len(serialized) == source_accounting_characters(
        batch,
        timezone=policy.timezone,
        max_characters=policy.batch_max_characters,
    )
    assert len(serialized) <= policy.batch_max_characters
    parts = [rollup_source_projection(event, timezone=policy.timezone) for event in batch]
    assert unbounded == "\n".join(parts)
    assert len(unbounded) == sum(len(part) for part in parts) + len(batch) - 1
    assert len(batch) < len(events) or len(unbounded) <= policy.batch_max_characters


def test_oversized_singleton_makes_progress_with_bounded_deterministic_source() -> None:
    policy = _policy(batch_max_events=3, batch_max_characters=80)
    huge = _external(1, "n" * 400)
    batch = take_batch((huge, _external(2, "tail")), policy)
    assert tuple(event.id for event in batch) == (1,)
    unbounded = serialize_compaction_source_events(batch, timezone=policy.timezone)
    serialized = bound_compaction_source_events(
        batch,
        timezone=policy.timezone,
        max_characters=policy.batch_max_characters,
    )
    assert len(unbounded) > policy.batch_max_characters
    assert len(serialized) == source_accounting_characters(
        batch,
        timezone=policy.timezone,
        max_characters=policy.batch_max_characters,
    )
    assert len(serialized) <= policy.batch_max_characters
    assert serialized != unbounded
    assert serialized != rollup_source_projection(huge, timezone=policy.timezone)
    full_projection = rollup_source_projection(huge, timezone=policy.timezone)
    assert projection_hash(huge) == hashlib.sha256(full_projection.encode("utf-8")).hexdigest()
    assert projection_hash(huge) != hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    assert "raw" not in serialized
    mixed = take_batch(
        (huge, _external(2, "n" * 400), _message(3, "human"), _external(4, "tail")),
        policy,
    )
    mixed_source = bound_compaction_source_events(
        mixed,
        timezone=policy.timezone,
        max_characters=policy.batch_max_characters,
    )
    assert len(mixed) == 1
    assert len(mixed_source) <= policy.batch_max_characters


def test_foreground_fit_uses_visible_message_count_not_raw_keepers() -> None:
    history = (
        _external(1, "storm"),
        _external(2, "storm"),
        _external(3, "storm"),
        _message(4, "visible"),
    )
    current = _message(5, "now")
    renderer_history = prompt_accounting_characters(history)
    view = _UncoveredPromptView(
        history_rows=history,
        rendered=(),
        record=current,
        fallback_event_id=current.id,
        current_characters=10,
        rendered_characters=renderer_history,
    )
    assert prompt_visible_event_count(view.history_rows) == 1
    assert ContextAssembler._uncovered_fits_window(view, event_limit=1, character_budget=10_000)
    assert not ContextAssembler._uncovered_fits_window(
        _UncoveredPromptView(
            history_rows=(_message(1), _message(2), _message(3)),
            rendered=(),
            record=current,
            fallback_event_id=current.id,
            current_characters=10,
            rendered_characters=4_000,
        ),
        event_limit=1,
        character_budget=10_000,
    )


async def test_mixed_coverage_is_contiguous_across_external_ids(database: Database) -> None:
    policy = _policy(raw_tail_events=1, trigger_events=2, batch_max_events=8)
    uow = ScopedEventLedgerUnitOfWork(database, config=policy)
    repository = ConversationRollupRepository(database, policy)
    service = ConversationRollupService(models=None, config=policy, timeout_seconds=0.1)
    scope = await _prepare_private(database)
    await uow.append(
        scope=scope,
        platform_message_id="human-1",
        sender_user_id="1001",
        direction="inbound",
        content="hello",
        occurred_at=_NOW,
    )
    await uow.append_external(
        scope=scope,
        platform_message_id="ext-1",
        source_plugin_id="github-monitor",
        external_source="github",
        external_event_key="evt-1",
        external_event_type="PushEvent",
        external_payload={"body": "raw-1"},
        external_target_id="1001",
        content="push one",
        occurred_at=_NOW + timedelta(seconds=1),
    )
    await uow.append(
        scope=scope,
        platform_message_id="human-2",
        sender_user_id="1001",
        direction="inbound",
        content="thanks",
        occurred_at=_NOW + timedelta(seconds=2),
    )
    await uow.append_external(
        scope=scope,
        platform_message_id="ext-2",
        source_plugin_id="github-monitor",
        external_source="github",
        external_event_key="evt-2",
        external_event_type="PushEvent",
        external_payload={"body": "raw-2"},
        external_target_id="1001",
        content="push two",
        occurred_at=_NOW + timedelta(seconds=3),
    )
    committed = await service.ensure_extractive_coverage(
        repository=repository,
        scope=scope,
        lease_seconds=30,
        max_batches=4,
    )
    assert committed >= 1
    snapshot = await repository.load_prompt_snapshot(scope)
    keeper_ids = tuple(event.id for event in snapshot.raw_events)
    assert keeper_ids
    assert keeper_ids == tuple(range(keeper_ids[0], keeper_ids[-1] + 1))
    assert snapshot.effective_coverage < keeper_ids[0] or snapshot.raw_events[0].id == keeper_ids[0]
    assert all(event.id > snapshot.effective_coverage for event in snapshot.raw_events)
    assert keeper_ids == tuple(range(keeper_ids[0], keeper_ids[-1] + 1))


async def test_storm_triggers_without_eating_protected_message_suffix(database: Database) -> None:
    policy = _policy(
        raw_tail_events=2,
        trigger_events=4,
        batch_max_events=8,
        batch_max_characters=100_000,
    )
    uow = ScopedEventLedgerUnitOfWork(database, config=policy)
    repository = ConversationRollupRepository(database, policy)
    scope = await _prepare_private(database, peer="1002")
    for index in range(1, 6):
        await uow.append_external(
            scope=scope,
            platform_message_id=f"storm-{index}",
            source_plugin_id="github-monitor",
            external_source="github",
            external_event_key=f"storm-{index}",
            external_event_type="PushEvent",
            external_payload={"n": index},
            external_target_id="1002",
            content=f"storm {index}",
            occurred_at=_NOW + timedelta(seconds=index),
        )
    await uow.append(
        scope=scope,
        platform_message_id="keep-a",
        sender_user_id="1002",
        direction="inbound",
        content="keep-a",
        occurred_at=_NOW + timedelta(seconds=10),
    )
    await uow.append_external(
        scope=scope,
        platform_message_id="between",
        source_plugin_id="github-monitor",
        external_source="github",
        external_event_key="between",
        external_event_type="PushEvent",
        external_payload={},
        external_target_id="1002",
        content="between",
        occurred_at=_NOW + timedelta(seconds=11),
    )
    await uow.append(
        scope=scope,
        platform_message_id="keep-b",
        sender_user_id="1002",
        direction="inbound",
        content="keep-b",
        occurred_at=_NOW + timedelta(seconds=12),
    )
    snapshot = await repository.load_prompt_snapshot(scope)
    start = protected_tail_start(snapshot.raw_events, policy)
    protected = snapshot.raw_events[start:]
    assert prompt_visible_event_count(protected) == 2
    assert {event.content for event in protected if event.event_kind == "message"} == {
        "keep-a",
        "keep-b",
    }
    claim = await repository.claim_next_job(lease_owner="storm", lease_seconds=30)
    assert claim is not None
    candidate = await repository.candidate_for_claim(claim)
    assert candidate is not None
    assert candidate.projection_characters == source_accounting_characters(
        candidate.events,
        timezone=policy.timezone,
        max_characters=policy.batch_max_characters,
    )
    assert all(event.content not in {"keep-a", "keep-b"} for event in candidate.events)
    serialized = serialize_compaction_source_events(candidate.events, timezone=policy.timezone)
    assert candidate.projection_characters == len(serialized)
    parts = [
        rollup_source_projection(event, timezone=policy.timezone) for event in candidate.events
    ]
    if len(candidate.events) > 1:
        assert len(serialized) == sum(len(part) for part in parts) + len(candidate.events) - 1
        assert serialized == "\n".join(parts)


async def test_external_append_is_zero_prompt_chars_and_does_not_force_wake(
    database: Database,
) -> None:
    policy = _policy(raw_tail_events=1, trigger_events=2, batch_max_events=8)
    uow = ScopedEventLedgerUnitOfWork(database, config=policy)
    repository = ConversationRollupRepository(database, policy)
    scope = await _prepare_private(database, peer="1003")
    first = await uow.append(
        scope=scope,
        platform_message_id="seed-1",
        sender_user_id="1003",
        direction="inbound",
        content="seed",
        occurred_at=_NOW,
    )
    await uow.append(
        scope=scope,
        platform_message_id="seed-2",
        sender_user_id="1003",
        direction="inbound",
        content="seed-two",
        occurred_at=_NOW + timedelta(seconds=1),
    )
    await uow.append(
        scope=scope,
        platform_message_id="seed-3",
        sender_user_id="1003",
        direction="inbound",
        content="seed-three",
        occurred_at=_NOW + timedelta(seconds=2),
    )
    from qq_ai_bot.conversation.canonical_rollup import drain_rollup_signals

    await drain_rollup_signals(database, policy)
    state, _rollup, job = await repository.status(scope)
    assert state is not None
    assert job is not None
    revision_before = int(job["signal_revision"])
    characters_before = state.uncovered_character_count
    events_before = state.uncovered_event_count
    appended = await uow.append_external(
        scope=scope,
        platform_message_id="quiet-ext",
        source_plugin_id="github-monitor",
        external_source="github",
        external_event_key="quiet",
        external_event_type="PushEvent",
        external_payload={"body": "no-wake"},
        external_target_id="1003",
        content="quiet external",
        occurred_at=_NOW + timedelta(seconds=3),
    )
    assert appended.created is True
    assert appended.job_signalled is False
    assert appended.scope.uncovered_character_count == characters_before
    assert appended.scope.uncovered_event_count == events_before + 1
    after, _rollup, job_after = await repository.status(scope)
    assert after is not None and job_after is not None
    assert int(job_after["signal_revision"]) == revision_before
    async with database.sessions() as session:
        stored = await session.get(
            CanonicalConversationRollupJobModel, first.event.canonical_conversation_id
        )
        assert stored is not None
        assert stored.signal_revision == revision_before


async def test_append_matches_recount_on_message_ruler(database: Database) -> None:
    policy = _policy()
    uow = ScopedEventLedgerUnitOfWork(database, config=policy)
    scope = await _prepare_private(database, peer="1004")
    human = await uow.append(
        scope=scope,
        platform_message_id="ruler-human",
        sender_user_id="1004",
        direction="inbound",
        content="visible human",
        occurred_at=_NOW,
    )
    await uow.append_external(
        scope=scope,
        platform_message_id="ruler-ext",
        source_plugin_id="github-monitor",
        external_source="github",
        external_event_key="ruler",
        external_event_type="PushEvent",
        external_payload={"body": "payload-must-not-count"},
        external_target_id="1004",
        content="x" * 500,
        occurred_at=_NOW + timedelta(seconds=1),
    )
    snapshot = await ConversationRollupRepository(database, policy).load_prompt_snapshot(scope)
    expected_events = len(snapshot.raw_events)
    expected_characters = durable_uncovered_characters(
        snapshot.raw_events,
        **_durable_kwargs(policy),
    )
    assert snapshot.scope.uncovered_event_count == expected_events
    assert snapshot.scope.uncovered_character_count == expected_characters
    assert snapshot.scope.uncovered_character_count == durable_uncovered_event_characters(
        human.event,
        **_durable_kwargs(policy),
    )
    async with database.immediate_session() as session:
        conversation = await session.get(
            CanonicalConversationModel, human.event.canonical_conversation_id
        )
        assert conversation is not None
        conversation.uncovered_character_count = 12_345
        recounted = await recount_canonical_uncovered(session, conversation, policy)
    assert recounted == (expected_events, expected_characters)
    async with database.immediate_session() as session:
        conversation = await session.get(
            CanonicalConversationModel, human.event.canonical_conversation_id
        )
        assert conversation is not None
        all_conversations = await recount_all_canonical_uncovered(session, policy)
    assert all_conversations.uncovered_event_count == expected_events
    assert all_conversations.uncovered_character_count == expected_characters
    assert all_conversations.conversation_count >= 1


async def test_durable_uncovered_characters_stay_equal_across_live_paths(
    database: Database,
) -> None:
    policy = _policy()
    kwargs = _durable_kwargs(policy)
    uow = ScopedEventLedgerUnitOfWork(database, config=policy)
    repository = ConversationRollupRepository(database, policy)
    scope = await _prepare_private(database, peer="1010")

    first = await uow.append(
        scope=scope,
        platform_message_id="dur-1",
        sender_user_id="1010",
        direction="inbound",
        content="hello",
        occurred_at=_NOW,
    )
    second = await uow.append(
        scope=scope,
        platform_message_id="dur-2",
        sender_user_id="1010",
        direction="inbound",
        content="again",
        occurred_at=_NOW + timedelta(seconds=1),
    )
    adjacent = (first.event, second.event)
    adjacent_total = durable_uncovered_characters(adjacent, **kwargs)
    grouped = prompt_accounting_characters(adjacent, **kwargs)
    assert adjacent_total == durable_uncovered_event_characters(
        first.event, **kwargs
    ) + durable_uncovered_event_characters(second.event, **kwargs)
    assert adjacent_total != grouped
    assert second.scope.uncovered_character_count == adjacent_total

    external = await uow.append_external(
        scope=scope,
        platform_message_id="dur-ext",
        source_plugin_id="github-monitor",
        external_source="github",
        external_event_key="dur",
        external_event_type="PushEvent",
        external_payload={"body": "ignored"},
        external_target_id="1010",
        content="storm",
        occurred_at=_NOW + timedelta(seconds=2),
    )
    third = await uow.append(
        scope=scope,
        platform_message_id="dur-3",
        sender_user_id="1010",
        direction="inbound",
        content="later",
        occurred_at=_NOW + timedelta(seconds=3),
    )
    mixed = (first.event, second.event, external.event, third.event)
    mixed_total = durable_uncovered_characters(mixed, **kwargs)
    assert durable_uncovered_event_characters(external.event, **kwargs) == 0
    assert mixed_total == adjacent_total + durable_uncovered_event_characters(third.event, **kwargs)
    assert third.scope.uncovered_character_count == mixed_total

    await uow.set_visual_summary(first.event.id, "a visual caption")
    snapshot = await repository.load_prompt_snapshot(scope)
    visual_total = durable_uncovered_characters(snapshot.raw_events, **kwargs)
    state, _rollup, _job = await repository.status(scope)
    assert state is not None
    assert state.uncovered_character_count == visual_total
    async with database.immediate_session() as session:
        conversation = await session.get(
            CanonicalConversationModel, first.event.canonical_conversation_id
        )
        assert conversation is not None
        recounted = await recount_canonical_uncovered(session, conversation, policy)
    assert recounted == (len(snapshot.raw_events), visual_total)
    state, _rollup, _job = await repository.status(scope)
    assert state is not None
    assert state.uncovered_character_count == visual_total

    fourth = await uow.append(
        scope=scope,
        platform_message_id="dur-4",
        sender_user_id="1010",
        direction="inbound",
        content="after-recount",
        occurred_at=_NOW + timedelta(seconds=4),
    )
    after = await repository.load_prompt_snapshot(scope)
    after_total = durable_uncovered_characters(after.raw_events, **kwargs)
    assert fourth.scope.uncovered_character_count == after_total
    async with database.immediate_session() as session:
        conversation = await session.get(
            CanonicalConversationModel, first.event.canonical_conversation_id
        )
        assert conversation is not None
        recounted_again = await recount_canonical_uncovered(session, conversation, policy)
    assert recounted_again == (len(after.raw_events), after_total)


async def test_compaction_service_sends_bounded_serialized_source() -> None:
    policy = _policy(batch_max_characters=80, summary_max_characters=2_000)
    huge = _message(1, "n" * 400)
    serialized = bound_compaction_source_events(
        (huge,),
        timezone=policy.timezone,
        max_characters=policy.batch_max_characters,
    )
    candidate_characters = source_accounting_characters(
        (huge,),
        timezone=policy.timezone,
        max_characters=policy.batch_max_characters,
    )

    class _Recorder:
        def __init__(self) -> None:
            self.request = None

        async def execute(self, task, request, **_kwargs):  # type: ignore[no-untyped-def]
            del task
            self.request = request
            return ChatResponse(content="compacted summary", latency_seconds=0)

    recorder = _Recorder()
    service = ConversationRollupService(
        models=recorder,  # type: ignore[arg-type]
        config=policy,
        timeout_seconds=1,
    )
    candidate = RollupCandidate(
        scope_id=1,
        generation=1,
        source_coverage=0,
        source_rollup_revision=0,
        previous_summary="",
        events=(huge,),
        event_count=1,
        projection_characters=candidate_characters,
        fingerprint="test",
    )
    _summary, kind = await service.summarize_candidate(candidate)
    assert kind is RollupKind.MODEL
    assert recorder.request is not None
    content = recorder.request.messages[1].content or ""
    marker = "New source events:\n"
    start = content.index(marker) + len(marker)
    end = content.index("\n\nCharacter limit:", start)
    sent = content[start:end]
    assert sent == serialized
    assert len(sent) == candidate.projection_characters
    assert len(sent) <= policy.batch_max_characters


async def test_candidate_projection_characters_equal_source_cost(database: Database) -> None:
    policy = _policy(
        raw_tail_events=1,
        trigger_events=2,
        batch_max_events=4,
        batch_max_characters=10_000,
    )
    uow = ScopedEventLedgerUnitOfWork(database, config=policy)
    repository = ConversationRollupRepository(database, policy)
    scope = await _prepare_private(database, peer="1005")
    await uow.append(
        scope=scope,
        platform_message_id="src-human",
        sender_user_id="1005",
        direction="inbound",
        content="human",
        occurred_at=_NOW,
    )
    await uow.append_external(
        scope=scope,
        platform_message_id="src-ext",
        source_plugin_id="github-monitor",
        external_source="github",
        external_event_key="src",
        external_event_type="PushEvent",
        external_payload={"body": "ignored"},
        external_target_id="1005",
        content="external summary",
        occurred_at=_NOW + timedelta(seconds=1),
    )
    await uow.append(
        scope=scope,
        platform_message_id="src-human-2",
        sender_user_id="1005",
        direction="inbound",
        content="later",
        occurred_at=_NOW + timedelta(seconds=2),
    )
    claim = await repository.claim_next_job(lease_owner="source", lease_seconds=30)
    assert claim is not None
    candidate = await repository.candidate_for_claim(claim)
    assert candidate is not None
    assert candidate.projection_characters == source_accounting_characters(
        candidate.events,
        timezone=policy.timezone,
        max_characters=policy.batch_max_characters,
    )
    serialized = bound_compaction_source_events(
        candidate.events,
        timezone=policy.timezone,
        max_characters=policy.batch_max_characters,
    )
    assert candidate.projection_characters == len(serialized)
    assert candidate.projection_characters <= policy.batch_max_characters
    assert candidate.projection_characters != prompt_accounting_characters(
        candidate.events,
        bot_display_name=policy.bot_display_name,
        timezone=policy.timezone,
    ) or all(event.event_kind == "message" for event in candidate.events)


def test_lightweight_backlog_ignores_stored_raw_event_count() -> None:
    settings = make_settings(
        "sqlite+aiosqlite:///:memory:",
        conversation_rollup_raw_tail_characters=100,
        conversation_rollup_trigger_characters=100,
        conversation_rollup_stop_characters=0,
    )
    assembler = ContextAssembler(
        settings=settings,
        ledger=MagicMock(),  # type: ignore[arg-type]
        people=MagicMock(),
        memory_context=MagicMock(),
        relationships=MagicMock(),
        time_service=MagicMock(),
        rollup_repository=MagicMock(),
        rollup_service=MagicMock(),
    )
    assert assembler._prompt_event_admit(event_limit=16, coverage_end=1) == 15
    inbound = InboundMessage(
        message_id="now",
        event_type="message",
        scope_type=ScopeType.PRIVATE,
        sender=SenderIdentity(user_id="1001"),
        text="now",
        bot_user_id="8000",
    )
    current = _message(99, "now")
    storm = (*tuple(_external(index) for index in range(1, 20)), _message(20))
    view = assembler._uncovered_prompt_view(
        storm,
        current_event_id=current.id,
        content="now",
        yuki_account_ids=inbound.yuki_account_ids,
        current_message_override=None,
        current_event=current,
    )
    assert view is not None
    assert prompt_visible_event_count(view.history_rows) == 1
    assert assembler._uncovered_fits_window(
        view,
        event_limit=assembler._prompt_event_admit(event_limit=16, coverage_end=1),
        character_budget=10_000,
    )


def _outbound(
    event_id: int, content: str, *, segments: tuple[dict[str, object], ...] = ()
) -> EventRecord:
    return EventRecord(
        id=event_id,
        bot_user_id="8000",
        platform_message_id=f"msg-{event_id}",
        scope_type=ScopeType.PRIVATE,
        sender_user_id="8000",
        direction="outbound",
        content=content,
        visual_summary="",
        segments=segments,
        occurred_at=_NOW + timedelta(seconds=event_id),
        event_kind="message",
        private_peer_user_id="1001",
        author_kind="yuki",
    )


def _mention_only(event_id: int) -> EventRecord:
    return EventRecord(
        id=event_id,
        bot_user_id="8000",
        platform_message_id=f"msg-{event_id}",
        scope_type=ScopeType.PRIVATE,
        sender_user_id="1001",
        direction="inbound",
        content="",
        visual_summary="",
        segments=({"type": "at", "data": {"qq": "8000"}},),
        occurred_at=_NOW + timedelta(seconds=event_id),
        event_kind="message",
        private_peer_user_id="1001",
        mentioned_user_ids=("8000",),
    )


def test_empty_and_silent_rows_are_not_prompt_visible() -> None:
    empty_in = _message(1, "")
    empty_out = _outbound(2, "")
    silent_image = _outbound(
        3,
        "",
        segments=({"type": "image", "data": {"file": "silent.jpg"}},),
    )
    mention = _mention_only(4)
    spoken = _message(5, "hello")
    assert is_prompt_visible_message(empty_in) is False
    assert is_prompt_visible_message(empty_out) is False
    assert is_prompt_visible_message(silent_image) is False
    assert is_prompt_visible_message(mention) is True
    assert is_prompt_visible_message(spoken) is True
    assert is_prompt_visible_message(_external(6)) is False


def test_protected_tail_keeps_last_n_actually_rendered_messages() -> None:
    policy = _policy(raw_tail_events=2)
    events = (
        _message(1, "eligible-human"),
        _message(2, ""),
        _outbound(3, ""),
        _outbound(4, "", segments=({"type": "image", "data": {"file": "x.jpg"}},)),
        _mention_only(5),
        _external(6, "between"),
        _message(7, "last-visible"),
    )
    start = protected_tail_start(events, policy)
    protected = events[start:]
    assert tuple(event.id for event in protected) == (5, 6, 7)
    assert prompt_visible_event_count(protected) == 2
    assert is_prompt_visible_message(events[4]) is True
    assert is_prompt_visible_message(events[6]) is True
    eligible = eligible_prefix(events, policy)
    assert tuple(event.id for event in eligible) == (1, 2, 3, 4)


class _CountingRollupService(ConversationRollupService):
    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.extractive_calls = 0

    async def ensure_extractive_coverage(self, **kwargs: object) -> int:
        self.extractive_calls += 1
        return await super().ensure_extractive_coverage(**kwargs)  # type: ignore[arg-type]


def _empty_retrieval() -> MemoryRetrievalResult:
    return MemoryRetrievalResult(
        blocks=(),
        hits=(),
        candidate_count=0,
        selected_count=0,
        query_hash="none",
        mode=MemoryRetrievalMode.RELEVANT,
    )


async def _assemble_private_turn(
    *,
    database: Database,
    settings,
    policy: RollupPolicyConfig,
    scope: ConversationScope,
    current,
    content: str,
    service: ConversationRollupService,
) -> object:
    people = MagicMock()
    people.aliases = AsyncMock(return_value=[])
    time_service = MagicMock()
    time_service.current = AsyncMock(
        return_value=TimeContext(utc=_NOW, local=_NOW, timezone=settings.default_timezone)
    )
    assembler = ContextAssembler(
        settings=settings,
        ledger=EventLedgerRepository(database),
        people=people,
        memory_context=MagicMock(),
        relationships=MagicMock(),
        time_service=time_service,
        rollup_repository=ConversationRollupRepository(database, policy),
        rollup_service=service,
    )
    runtime = MagicMock()
    runtime.context.local_event_limit = settings.local_context_event_limit
    inbound = InboundMessage(
        message_id=current.platform_message_id,
        event_type="message",
        scope_type=ScopeType.PRIVATE,
        sender=SenderIdentity(user_id=current.sender_user_id),
        text=content,
        bot_user_id=scope.bot_user_id,
    )
    state, _rollup, _job = await ConversationRollupRepository(database, policy).status(scope)
    assert state is not None
    return await assembler.assemble(
        inbound=inbound,
        identity=scope,
        profile=UserProfileSnapshot(
            user_id=current.sender_user_id,
            scope_type=ScopeType.PRIVATE,
            nickname="Ada",
        ),
        turn=ConversationTurnSnapshot(
            scope_id=state.id,
            scope_key=state.scope.key,
            generation=state.generation,
            trigger_event_id=current.id,
            coordinator_version=1,
        ),
        content=content,
        runtime=runtime,
        memory_retrieval=_empty_retrieval(),
        persist_memory_exposure=False,
    )


@pytest.mark.asyncio
async def test_assemble_ignores_durable_watermark_when_grouped_fits(
    database: Database,
) -> None:
    settings = make_settings(
        database.url,
        relationship_enabled=False,
        conversation_rollup_raw_tail_events=128,
        conversation_rollup_trigger_events=384,
        conversation_rollup_stop_events=0,
        conversation_rollup_raw_tail_characters=20_480,
        conversation_rollup_trigger_characters=81_920,
        conversation_rollup_stop_characters=0,
        conversation_rollup_foreground_max_batches=4,
        local_context_event_limit=2_048,
        max_context_characters=131_072,
    )
    policy = RollupPolicyConfig(
        raw_tail_events=settings.conversation_rollup_raw_tail_events,
        raw_tail_characters=settings.conversation_rollup_raw_tail_characters,
        trigger_events=settings.conversation_rollup_trigger_events,
        trigger_characters=settings.conversation_rollup_trigger_characters,
        stop_events=settings.conversation_rollup_stop_events,
        stop_characters=settings.conversation_rollup_stop_characters,
        bot_display_name=settings.bot_display_name,
        timezone=settings.default_timezone,
    )
    uow = ScopedEventLedgerUnitOfWork(database, config=policy)
    repository = ConversationRollupRepository(database, policy)
    service = _CountingRollupService(models=None, config=policy, timeout_seconds=0.1)
    scope = await _prepare_private(database, peer="1101")
    body = "s" * 780
    last = None
    for index in range(1, 130):
        last = await uow.append(
            scope=scope,
            platform_message_id=f"same-{index}",
            sender_user_id="1101",
            direction="inbound",
            content=body,
            occurred_at=_NOW + timedelta(seconds=index),
        )
    assert last is not None
    snapshot = await repository.load_prompt_snapshot(scope, before_event_id=last.event.id)
    admit = (
        settings.conversation_rollup_raw_tail_characters
        + settings.conversation_rollup_trigger_characters
    )
    durable = durable_uncovered_characters(
        snapshot.raw_events,
        bot_display_name=policy.bot_display_name,
        timezone=policy.timezone,
    )
    grouped = prompt_accounting_characters(
        snapshot.raw_events,
        bot_display_name=policy.bot_display_name,
        timezone=policy.timezone,
    )
    assert durable > admit
    assert grouped <= admit
    assert eligible_prefix(snapshot.raw_events, policy) == ()
    before_state, before_rollup, _job = await repository.status(scope)
    assert before_state is not None
    assembled = await _assemble_private_turn(
        database=database,
        settings=settings,
        policy=policy,
        scope=scope,
        current=last.event,
        content=body,
        service=service,
    )
    after_state, after_rollup, _job = await repository.status(scope)
    assert after_state is not None
    assert service.extractive_calls == 0
    assert assembled.prompt_effective_coverage == (
        before_rollup.covered_through_event_id if before_rollup else 0
    )
    assert after_state.uncovered_event_count == before_state.uncovered_event_count
    assert after_state.uncovered_character_count == before_state.uncovered_character_count
    assert (after_rollup is None) == (before_rollup is None)
    if before_rollup is not None and after_rollup is not None:
        assert after_rollup.revision == before_rollup.revision
        assert after_rollup.covered_through_event_id == before_rollup.covered_through_event_id


@pytest.mark.asyncio
async def test_assemble_mixed_sender_window_does_not_fail_in_preflight(
    database: Database,
) -> None:
    settings = make_settings(
        database.url,
        relationship_enabled=False,
        conversation_rollup_raw_tail_events=128,
        conversation_rollup_trigger_events=384,
        conversation_rollup_stop_events=0,
        conversation_rollup_raw_tail_characters=20_480,
        conversation_rollup_trigger_characters=81_920,
        conversation_rollup_stop_characters=0,
        conversation_rollup_foreground_max_batches=4,
        local_context_event_limit=2_048,
        max_context_characters=131_072,
    )
    policy = RollupPolicyConfig(
        raw_tail_events=settings.conversation_rollup_raw_tail_events,
        raw_tail_characters=settings.conversation_rollup_raw_tail_characters,
        trigger_events=settings.conversation_rollup_trigger_events,
        trigger_characters=settings.conversation_rollup_trigger_characters,
        stop_events=settings.conversation_rollup_stop_events,
        stop_characters=settings.conversation_rollup_stop_characters,
        bot_display_name=settings.bot_display_name,
        timezone=settings.default_timezone,
    )
    uow = ScopedEventLedgerUnitOfWork(database, config=policy)
    repository = ConversationRollupRepository(database, policy)
    service = _CountingRollupService(models=None, config=policy, timeout_seconds=0.1)
    scope = await _prepare_private(database, peer="1102")
    from qq_ai_bot.identity.canonical_repository import ensure_person

    async with database.sessions() as session, session.begin():
        await ensure_person(session, "1103", now=_NOW)
    body = "m" * 900
    last = None
    for index in range(1, 130):
        sender = "1102" if index % 2 else "1103"
        last = await uow.append(
            scope=scope,
            platform_message_id=f"mix-{index}",
            sender_user_id=sender,
            direction="inbound",
            content=body,
            occurred_at=_NOW + timedelta(seconds=index),
        )
    assert last is not None
    snapshot = await repository.load_prompt_snapshot(scope, before_event_id=last.event.id)
    admit = (
        settings.conversation_rollup_raw_tail_characters
        + settings.conversation_rollup_trigger_characters
    )
    grouped = prompt_accounting_characters(
        snapshot.raw_events,
        bot_display_name=policy.bot_display_name,
        timezone=policy.timezone,
    )
    assert grouped > admit
    assert grouped <= settings.max_context_characters
    assembled = await _assemble_private_turn(
        database=database,
        settings=settings,
        policy=policy,
        scope=scope,
        current=last.event,
        content=body,
        service=service,
    )
    assert assembled.prompt_effective_coverage == 0
    assert service.extractive_calls == 0
