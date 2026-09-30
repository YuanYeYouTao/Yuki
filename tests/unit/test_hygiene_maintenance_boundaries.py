"""Explicit FTS maintenance and bounded provenance snapshot upgrades."""

from __future__ import annotations

import pytest
from sqlalchemy import event, select, text, update
from sqlalchemy.exc import OperationalError
from tests.unit.test_embedding_claim_boundaries import _jobs

from qq_ai_bot.memory.quality import hygiene as hygiene_module
from qq_ai_bot.memory.quality.hygiene import MemoryProvenanceHygiene
from qq_ai_bot.persistence.models import MemoryFactModel, MemoryFactStateEventModel


async def test_regular_hygiene_preserves_fts_full_rebuild_for_explicit_window(database):
    await _jobs(database)
    async with database.immediate_session() as writer:
        await writer.execute(
            text("INSERT INTO memory_facts_fts(memory_facts_fts) VALUES ('delete-all')")
        )
    hygiene = MemoryProvenanceHygiene(database)
    plan = await hygiene.scan()
    assert plan.rebuild_fts
    statements = []

    def trace(_conn, _cursor, statement, _params, _ctx, _many):
        statements.append(statement)

    event.listen(database.engine.sync_engine, "before_cursor_execute", trace)
    try:
        assert (await hygiene.apply(plan.fingerprint)).rebuild_fts
        assert not any("VALUES ('rebuild')" in sql for sql in statements)
        plan = await hygiene.scan()
        assert plan.rebuild_fts
        await hygiene.rebuild_fts(plan.fingerprint)
        assert sum("VALUES ('rebuild')" in sql for sql in statements) == 1
        assert not (await hygiene.scan()).rebuild_fts
    finally:
        event.remove(database.engine.sync_engine, "before_cursor_execute", trace)


async def test_provenance_page_discards_snapshot_after_competing_source_change(
    database, monkeypatch
):
    await _jobs(database)
    async with database.immediate_session() as writer:
        await writer.execute(update(MemoryFactModel).values(source_type="automatic"))
    async with database.sessions() as reader:
        fact_id = await reader.scalar(select(MemoryFactModel.id))
    original = hygiene_module.facts_with_valid_event_evidence

    async def change_after_read(reader, ids):
        valid = await original(reader, ids)
        async with database.immediate_session() as writer:
            await writer.execute(
                update(MemoryFactModel)
                .where(MemoryFactModel.id == fact_id)
                .values(source_type="explicit")
            )
        return valid

    monkeypatch.setattr(hygiene_module, "facts_with_valid_event_evidence", change_after_read)
    with pytest.raises(OperationalError, match="locked"):
        await MemoryProvenanceHygiene(database)._invalidate_page((fact_id,))
    async with database.sessions() as reader:
        fact = await reader.get(MemoryFactModel, fact_id)
        assert fact.status == "active" and fact.source_type == "explicit"
        assert not tuple(await reader.scalars(select(MemoryFactStateEventModel)))
