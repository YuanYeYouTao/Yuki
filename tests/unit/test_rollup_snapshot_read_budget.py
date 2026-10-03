"""Temporary prompt read budgets preserve whole events and real history grouping."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from qq_ai_bot.conversation.rollup.models import RollupPolicyConfig
from qq_ai_bot.conversation.rollup.repository import ConversationRollupRepository
from qq_ai_bot.domain.conversations import ConversationScope
from qq_ai_bot.event_prompt import ChatEventPromptRenderer
from qq_ai_bot.identity.canonical_repository import ensure_person, ensure_presence
from qq_ai_bot.model_runtime.capacity import estimate_text_tokens
from qq_ai_bot.persistence.models import ChatEventModel
from qq_ai_bot.persistence.repository_helpers import _event_record
from qq_ai_bot.persistence.scoped_event_uow import ScopedEventLedgerUnitOfWork


def _cost(events, policy):
    messages = ChatEventPromptRenderer(
        events, bot_display_name=policy.bot_display_name, timezone=policy.timezone
    ).main_agent_history(events)
    return sum(estimate_text_tokens(message.content or "") + 8 for _, _, message in messages)


async def _seed(database, *, count=80, content="x", outbound=True):
    policy = RollupPolicyConfig(context_token_budget=20, batch_max_events=16)
    scope = ConversationScope.private("8000", "1001")
    async with database.immediate_session() as session:
        await ensure_presence(session, "8000")
        await ensure_person(session, "1001")
    writer = ScopedEventLedgerUnitOfWork(database, config=policy)
    for index in range(count):
        await writer.append(
            scope=scope,
            platform_message_id=f"grouped-{index}",
            sender_user_id="8000" if outbound else "1001",
            direction="outbound" if outbound else "inbound",
            content=content,
            occurred_at=datetime(2026, 10, 3, tzinfo=UTC) + timedelta(seconds=index),
        )
    async with database.sessions() as reader:
        rows = (await reader.scalars(select(ChatEventModel).order_by(ChatEventModel.id))).all()
        events = tuple(_event_record(row) for row in rows)
    return ConversationRollupRepository(database, policy), scope, events


@pytest.mark.asyncio
async def test_temporary_read_budget_uses_grouped_cost_and_does_not_raise_background_policy(
    database,
):
    repository, scope, events = await _seed(database)
    policy = repository.config
    grouped = _cost(events, policy)
    isolated = sum(_cost((event,), policy) for event in events)
    assert grouped < isolated
    small = await repository.load_prompt_snapshot(scope)
    assert not small.raw_complete and len(small.raw_events) < len(events)
    complete = await repository.load_prompt_snapshot(scope, token_budget=grouped)
    assert complete.raw_complete and complete.raw_events == events
    assert _cost(complete.raw_events, policy) == grouped
    assert repository.config == policy
    assert (await repository._scope_policy(scope)).context_token_budget == 20
    assert await repository.load_prompt_snapshot(scope) == small
    claim = await repository.claim_scope_for_foreground(
        scope, lease_owner="background", lease_seconds=30
    )
    candidate = await repository.candidate_for_claim(claim)
    assert candidate is not None and candidate.policy.context_token_budget == 20


@pytest.mark.asyncio
async def test_read_budget_cuts_only_whole_contiguous_events_at_page_boundary(database):
    repository, scope, events = await _seed(
        database, count=45, content="source " * 6, outbound=False
    )
    budget = _cost(events[-20:], repository.config)
    snapshot = await repository.load_prompt_snapshot(scope, token_budget=budget)
    assert not snapshot.raw_complete
    ids = tuple(event.id for event in snapshot.raw_events)
    assert ids == tuple(event.id for event in events[-len(ids) :])
    assert len(ids) >= 20
    assert _cost(snapshot.raw_events, repository.config) <= budget
    assert all(event.content == "source " * 6 for event in snapshot.raw_events)
    before = await repository.load_prompt_snapshot(
        scope, before_event_id=events[30].id, token_budget=100000
    )
    assert before.raw_complete and before.raw_events == events[:30]


@pytest.mark.asyncio
async def test_single_large_event_is_not_truncated_and_actual_read_can_prove_complete(database):
    repository, scope, events = await _seed(database, count=1, content="large source " * 1000)
    limited = await repository.load_prompt_snapshot(scope, token_budget=1)
    assert limited.raw_events == events and not limited.raw_complete
    complete = await repository.load_prompt_snapshot(
        scope, token_budget=_cost(events, repository.config)
    )
    assert complete.raw_events == events and complete.raw_complete
