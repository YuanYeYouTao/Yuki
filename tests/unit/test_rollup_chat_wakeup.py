"""Recovery coalesces chat demand, not replayable tool executions."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from qq_ai_bot.domain.conversations import ConversationScope
from qq_ai_bot.persistence.event_repository import ConversationReadVersion
from qq_ai_bot.services.rollup_wakeup import RollupWakeups


def version():
    return ConversationReadVersion(ConversationScope.group("1", "2"), "conversation", 1, 0)


@pytest.mark.asyncio
async def test_finished_before_registration_is_not_lost(database):
    wake = RollupWakeups(database)
    wake._status = AsyncMock(return_value=(True, True))
    ticket = wake.enter("conversation")
    wake.notify("conversation")
    wake.leave(ticket, deferred=True)
    assert await wake.wait(version(), ticket)
    assert not wake.states


@pytest.mark.asyncio
async def test_multiple_interrupted_turns_wake_only_latest(database):
    wake = RollupWakeups(database)
    wake._status = AsyncMock(return_value=(True, False))
    first, latest = wake.enter("conversation"), wake.enter("conversation")
    wake.leave(first, deferred=True)
    waiter = asyncio.create_task(wake.wait(version(), first))
    await asyncio.sleep(0)
    wake.leave(latest, deferred=True)
    assert not await waiter
    wake._status.return_value = (True, True)
    assert await wake.wait(version(), latest)
    assert not wake.states


@pytest.mark.asyncio
async def test_normal_reply_takes_over_without_extra_wakeup(database):
    wake = RollupWakeups(database)
    wake._status = AsyncMock(return_value=(True, True))
    old = wake.enter("conversation")
    wake.leave(old, deferred=True)
    normal = wake.enter("conversation")
    waiter = asyncio.create_task(wake.wait(version(), old))
    await asyncio.sleep(0)
    assert not waiter.done()
    wake.handled(normal)
    wake.leave(normal)
    assert not await waiter
    assert not wake.states


@pytest.mark.asyncio
@pytest.mark.parametrize("close", [False, True])
async def test_failed_rollup_wait_is_bounded_and_releases_state(database, close):
    wake = RollupWakeups(database, timeout=0.01)
    wake._status = AsyncMock(return_value=(True, False))
    ticket = wake.enter("conversation")
    wake.leave(ticket, deferred=True)
    if close:
        wake.close()
    assert not await wake.wait(version(), ticket)
    assert not wake.states


@pytest.mark.asyncio
async def test_source_edit_without_rollup_does_not_retry(database):
    wake = RollupWakeups(database)
    wake._status = AsyncMock(return_value=(False, True))
    ticket = wake.enter("conversation")
    wake.leave(ticket, deferred=True)
    assert not await wake.wait(version(), ticket)


@pytest.mark.asyncio
async def test_real_database_distinguishes_rollup_from_reset(database):
    from datetime import UTC, datetime

    from qq_ai_bot.conversation.canonical_db_models import (
        CanonicalConversationModel,
        CanonicalConversationRollupModel,
    )
    from qq_ai_bot.conversation.hydrate import ensure_canonical_conversation
    from qq_ai_bot.identity.canonical_repository import ensure_space
    from qq_ai_bot.persistence.event_repository import EventLedgerRepository

    scope = ConversationScope.group("1", "2")
    async with database.sessions() as session, session.begin():
        space = await ensure_space(session, "2")
        conversation = await ensure_canonical_conversation(
            session, kind="space", primary_scope_key=scope.key, space_id=space
        )
    source, _ = await EventLedgerRepository(database).read_scope_context(scope, limit=10)
    wake = RollupWakeups(database)
    assert await wake._status(source) == (False, True)
    async with database.sessions() as session, session.begin():
        session.add(
            CanonicalConversationRollupModel(
                conversation_id=conversation.conversation_id,
                generation=source.generation,
                covered_through_event_id=0,
                summary_text="summary",
                summary_kind="model",
                source_fingerprint="0" * 64,
                revision=1,
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
        )
    assert await wake._status(source) == (True, True)
    async with database.sessions() as session, session.begin():
        row = await session.get(CanonicalConversationModel, conversation.conversation_id)
        row.generation += 1
    assert await wake._status(source) == (False, False)


@pytest.mark.asyncio
async def test_mutation_or_new_owner_blocks_chat_replay():
    from qq_ai_bot.services.turn_coordinator import ConversationTurnCoordinator

    coordinator = ConversationTurnCoordinator()
    token = await coordinator.notify_message("conversation")
    assert coordinator.can_retry_uncommitted(token)
    await coordinator.mark_mutation_started(token)
    assert not coordinator.can_retry_uncommitted(token)
    latest = await coordinator.notify_message("conversation")
    assert not coordinator.can_retry_uncommitted(token)
    assert coordinator.can_retry_uncommitted(latest)


@pytest.mark.asyncio
async def test_autonomous_observation_does_not_repeat_consumed_batch(monkeypatch):
    from types import SimpleNamespace

    from qq_ai_bot.services import autonomous_groups as module

    service = object.__new__(module.AutonomousGroupService)
    state = SimpleNamespace(
        revision=1,
        consumed_revision=-1,
        changed=asyncio.Event(),
        message=SimpleNamespace(
            group_id="group", conversation_id="conversation", source_event_id=10
        ),
    )
    service._states = {"scope": state}
    service._runtime_config = SimpleNamespace(snapshot=AsyncMock())
    monkeypatch.setattr(
        module,
        "_conversation_policy",
        lambda _: SimpleNamespace(autonomous_enabled=True, autonomous_debounce_seconds=0.001),
    )
    calls = []

    async def run(*args):
        calls.append(args)
        state.revision += 1
        state.changed.set()
        service.consume_rollup_history("conversation", 10)

    service._run_latest = run
    await service._after_silence("scope")
    assert len(calls) == 1
    assert not state.changed.is_set()
    state.message.source_event_id = 11
    state.revision += 1
    service.consume_rollup_history("conversation", 10)
    assert state.consumed_revision < state.revision
