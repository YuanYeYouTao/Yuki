"""An empty prepared event set still proves the exact selected SQL window."""

import pytest
from sqlalchemy import select
from tests.unit.test_memory_v2 import _append_event

from yuki.memory.self_reflection.repository import _events_current, _prepare_event_sources
from yuki.persistence.models import ChatEventModel
from yuki.persistence.repositories import EventLedgerRepository


@pytest.mark.parametrize(
    ("limit", "offset", "still_current"), [(1, 0, False), (0, 0, True), (1, 1, False)]
)
async def test_empty_source_proof_observes_new_members_in_the_original_window(
    database, limit, offset, still_current
):
    ledger = EventLedgerRepository(database)
    baseline = await _append_event(ledger, message_id="before-source-window", group_id="2001")
    if offset:
        await _append_event(ledger, message_id="before-source-offset", group_id="2001")
    query = (
        select(ChatEventModel)
        .where(ChatEventModel.id > baseline.id)
        .order_by(ChatEventModel.id.desc())
        .limit(limit)
        .offset(offset)
    )
    async with database.read_session() as reader:
        rows = tuple(await reader.scalars(query))
        assert not rows
        prepared = _prepare_event_sources(reader, rows)
    await _append_event(ledger, message_id="new-source-member", group_id="2001")
    async with database.immediate_session() as writer:
        assert await _events_current(writer, query, prepared) is still_current
