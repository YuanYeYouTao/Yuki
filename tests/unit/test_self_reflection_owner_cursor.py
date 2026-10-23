"""Legacy scope hashes keep the original owner cursor and unfinished run."""

import pytest
from sqlalchemy import select
from tests.unit.test_memory_v2 import _append_event

from yuki.memory.self_reflection.repository import SelfReflectionRepository
from yuki.persistence.models import (
    MemorySelfReflectionRunModel as Run,
)
from yuki.persistence.models import (
    MemorySelfReflectionStateModel as State,
)
from yuki.persistence.repositories import EventLedgerRepository


@pytest.mark.parametrize("group_id", [None, "2001"])
async def test_scan_keeps_legacy_owner_cursor_and_retries_original_gap(database, group_id):
    repository = SelfReflectionRepository(database)
    ledger = EventLedgerRepository(database)
    await repository.scan_new_events()
    first = await _append_event(
        ledger, message_id="original-reflection", group_id=group_id, content="original evidence"
    )
    await repository.scan_new_events()
    legacy_hash = "e" * 64
    async with database.immediate_session() as writer:
        state = (await writer.scalars(select(State))).one()
        identity, original_cursor = state.id, state.last_event_id
        # A migrated owner keeps its historical hash and the runs that used it.
        state.conversation_key_hash = legacy_hash
    claim = dict(
        event_threshold=1,
        character_threshold=1,
        max_wait_seconds=0,
        max_events=100,
        max_characters=16000,
        force=True,
    )
    (original,) = await repository.claim_due(scheduled_slot="original", **claim)
    await repository.recover_interrupted(original.run_id, "preempted")
    async with database.read_session() as reader:
        original_run = (await reader.execute(select(Run.__table__))).one()
    latest = await _append_event(
        ledger, message_id="later-reflection", group_id=group_id, content="new evidence"
    )
    assert await repository.scan_new_events() == 1
    assert await repository.scan_new_events() == 0
    async with database.read_session() as reader:
        state = (await reader.scalars(select(State))).one()
        assert state.id == identity
        assert state.conversation_key_hash == legacy_hash
        assert state.last_event_id == original_cursor
        assert state.latest_event_id == latest.id
        assert state.pending_events == 2
        assert state.pending_characters == len("original evidence") + len("new evidence")
        assert (await reader.execute(select(Run.__table__))).one() == original_run
        assert [gap.id for gap in await reader.execute(repository._gap_query(state))] == [
            original.run_id
        ]
    (retry,) = await repository.claim_due(scheduled_slot="retry", **claim)
    assert retry.run_id == original.run_id
    assert [event.id for event in retry.events] == [first.id]
    await repository.complete(retry, proposals=0, committed=0)
    async with database.read_session() as reader:
        state = (await reader.scalars(select(State))).one()
        assert state.id == identity and state.conversation_key_hash == legacy_hash
        assert state.last_event_id == first.id and state.latest_event_id == latest.id
        assert state.pending_events == 1 and state.pending_characters == len("new evidence")
