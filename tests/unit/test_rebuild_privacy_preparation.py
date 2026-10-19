"""Exact alias sanitation and stale-directory rollback without writer JSON."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event, func, select, update
from tests.unit.test_rebuild_receipt_finalization import _seed

from yuki.identity.canonical_repository import ensure_person
from yuki.memory.rebuild.models import MemoryRebuildSelection
from yuki.memory.rebuild.repository import MemoryRebuildRepository
from yuki.persistence.models import MemoryRebuildProposalModel, MemoryRebuildRunModel


def _selection(**values):
    encoded = MemoryRebuildSelection(**values).model_dump_json()
    return dict(selection_json=encoded, selection_hash=hashlib.sha256(encoded.encode()).hexdigest())


async def _persons(database, *accounts):
    async with database.immediate_session() as writer:
        return tuple([await ensure_person(writer, account) for account in accounts])


async def test_aliases_prepared_together_exact_selection_no_writer_json(database):
    seeded = await _seed(database, 1, proposals=True)
    first, second, unrelated = await _persons(database, "1001", "1002", "10011")
    async with database.immediate_session() as writer:
        await writer.execute(
            update(MemoryRebuildRunModel)
            .where(MemoryRebuildRunModel.id == seeded.run_id)
            .values(**_selection(person_ids=(first, second, unrelated)))
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
        assert selection.person_ids == (unrelated,)
        assert run.status != "cancelled"
        assert not tuple(await reader.scalars(select(MemoryRebuildProposalModel)))


async def test_new_matching_selection_is_prepared_on_current_snapshot_without_catalogue_gate(
    database,
):
    first, second = await _seed(database, 1, proposals=True), await _seed(database, 1)
    selected, unrelated = await _persons(database, "1001", "777771")
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
            .values(**_selection(person_ids=(unrelated,)), updated_at=now - timedelta(days=1))
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
            .values(**_selection(person_ids=(selected,)), updated_at=now)
        )
    assert await maxima() == before
    assert await repo.forget_people(("1001",), prepared=prepared) >= 2
    async with database.sessions() as reader:
        assert (
            await reader.scalar(select(func.count()).select_from(MemoryRebuildProposalModel)) == 0
        )
        sanitized = await reader.get(MemoryRebuildRunModel, second.run_id)
        assert (
            selected
            not in MemoryRebuildSelection.model_validate_json(sanitized.selection_json).person_ids
        )
        assert MemoryRebuildSelection.model_validate_json(sanitized.selection_json).event_ids == ()
        assert sanitized.status == "committing"


async def test_shared_privacy_writer_requires_preparation_and_rolls_back_as_one_unit(database):
    seeded = await _seed(database, 1, proposals=True)
    (selected,) = await _persons(database, "1001")
    async with database.immediate_session() as writer:
        await writer.execute(
            update(MemoryRebuildRunModel)
            .where(MemoryRebuildRunModel.id == seeded.run_id)
            .values(**_selection(person_ids=(selected,)))
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
        assert MemoryRebuildSelection.model_validate_json(run.selection_json).person_ids == (
            selected,
        )
        assert (
            await reader.scalar(select(func.count()).select_from(MemoryRebuildProposalModel)) == 1
        )


async def test_forget_erases_original_native_selection_body_without_replacing_query_or_budget(
    database,
):
    seeded = await _seed(database, 1)
    original = json.dumps({"sender_user_ids": ["1001"], "maximum_events": 25})
    digest = hashlib.sha256(original.encode()).hexdigest()
    async with database.immediate_session() as writer:
        run = await writer.get(MemoryRebuildRunModel, seeded.run_id)
        selection_json, selection_hash = run.selection_json, run.selection_hash
        run.original_selection_json = original
        run.original_selection_hash = digest
        run.extraction_requests = 4
    repository = MemoryRebuildRepository(database)
    assert await repository.forget_people(("1001",)) == 1
    async with database.sessions() as reader:
        run = await reader.get(MemoryRebuildRunModel, seeded.run_id)
        assert run.original_selection_json is None and run.original_selection_hash == digest
        assert (run.selection_json, run.selection_hash) == (selection_json, selection_hash)
        assert run.status == "committing" and run.extraction_requests == 4
