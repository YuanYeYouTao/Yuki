"""Exact alias sanitation and stale-directory rollback without writer JSON."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event, func, select, update
from tests.unit.test_rebuild_receipt_finalization import _seed

from qq_ai_bot.memory.rebuild.models import MemoryRebuildSelection
from qq_ai_bot.memory.rebuild.repository import MemoryRebuildRepository
from qq_ai_bot.persistence.models import MemoryRebuildProposalModel, MemoryRebuildRunModel


def _selection(**values):
    encoded = MemoryRebuildSelection(**values).model_dump_json()
    return dict(selection_json=encoded, selection_hash=hashlib.sha256(encoded.encode()).hexdigest())


async def test_aliases_prepared_together_exact_selection_no_writer_json(database):
    seeded = await _seed(database, 1, proposals=True)
    async with database.immediate_session() as writer:
        await writer.execute(
            update(MemoryRebuildRunModel)
            .where(MemoryRebuildRunModel.id == seeded.run_id)
            .values(**_selection(sender_user_ids=("1001", "1002", "10011")))
        )
    repo = MemoryRebuildRepository(database)
    prepared = await repo.prepare_forget_people(("1002", "1001"))
    statements = []

    def trace(_conn, _cursor, statement, _params, _ctx, _many):
        statements.append(statement)

    event.listen(database.engine.sync_engine, "before_cursor_execute", trace)
    try:
        async with database.immediate_session() as writer:
            assert (
                await repo.forget_people(("1001", "1002"), prepared=prepared, session=writer) == 2
            )
        reads = [sql for sql in statements if sql.lstrip().upper().startswith("SELECT")]
        assert reads and all("selection_json" not in sql for sql in reads)
    finally:
        event.remove(database.engine.sync_engine, "before_cursor_execute", trace)
    async with database.sessions() as reader:
        run = await reader.get(MemoryRebuildRunModel, seeded.run_id)
        selection = MemoryRebuildSelection.model_validate_json(run.selection_json)
        assert selection.sender_user_ids == ("10011",)
        assert run.status != "cancelled"
        assert not tuple(await reader.scalars(select(MemoryRebuildProposalModel)))


async def test_new_matching_selection_detected_even_when_catalogue_maxima_unchanged(database):
    first, second = await _seed(database, 1, proposals=True), await _seed(database, 1)
    now = datetime.now(UTC)
    async with database.immediate_session() as writer:
        await writer.execute(
            update(MemoryRebuildRunModel)
            .where(MemoryRebuildRunModel.id == first.run_id)
            .values(updated_at=now + timedelta(days=3))
        )
        await writer.execute(
            update(MemoryRebuildRunModel)
            .where(MemoryRebuildRunModel.id == second.run_id)
            .values(**_selection(sender_user_ids=("9999",)), updated_at=now - timedelta(days=1))
        )

    async def maxima():
        async with database.sessions() as reader:
            return tuple(
                (
                    await reader.execute(
                        select(
                            func.count(MemoryRebuildRunModel.id),
                            func.max(MemoryRebuildRunModel.id),
                            func.max(MemoryRebuildRunModel.updated_at),
                        )
                    )
                ).one()
            )

    before = await maxima()
    repo = MemoryRebuildRepository(database)
    prepared = await repo.prepare_forget_people(("1001",))
    async with database.immediate_session() as writer:
        await writer.execute(
            update(MemoryRebuildRunModel)
            .where(MemoryRebuildRunModel.id == second.run_id)
            .values(**_selection(sender_user_ids=("1001",)), updated_at=now)
        )
    assert await maxima() == before
    with pytest.raises(ValueError, match="preparation_changed"):
        await repo.forget_people(("1001",), prepared=prepared)
    async with database.sessions() as reader:
        assert (
            await reader.scalar(select(func.count()).select_from(MemoryRebuildProposalModel)) == 1
        )


async def test_shared_privacy_writer_requires_preparation_and_rolls_back_as_one_unit(database):
    seeded = await _seed(database, 1, proposals=True)
    async with database.immediate_session() as writer:
        await writer.execute(
            update(MemoryRebuildRunModel)
            .where(MemoryRebuildRunModel.id == seeded.run_id)
            .values(**_selection(sender_user_ids=("1001",)))
        )
    repo = MemoryRebuildRepository(database)
    prepared = await repo.prepare_forget_people(("1001",))
    with pytest.raises(ValueError, match="preparation_required"):
        async with database.immediate_session() as writer:
            await repo.forget_people(("1001",), session=writer)
    with pytest.raises(RuntimeError, match="outer privacy failure"):
        async with database.immediate_session() as writer:
            await repo.forget_people(("1001",), prepared=prepared, session=writer)
            raise RuntimeError("outer privacy failure")
    async with database.sessions() as reader:
        run = await reader.get(MemoryRebuildRunModel, seeded.run_id)
        assert MemoryRebuildSelection.model_validate_json(run.selection_json).sender_user_ids == (
            "1001",
        )
        assert (
            await reader.scalar(select(func.count()).select_from(MemoryRebuildProposalModel)) == 1
        )
