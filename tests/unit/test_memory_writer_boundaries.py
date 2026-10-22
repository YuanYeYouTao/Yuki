"""Real WAL empty maintenance reads, fair source windows, and recovery CAS."""

from contextlib import asynccontextmanager

from sqlalchemy import delete, event
from tests.conftest import make_settings

from yuki.memory.maintenance import MemoryMaintenanceWorker
from yuki.memory.models import MemoryFactCreate
from yuki.memory.repository import MemoryFactRepository
from yuki.memory.service import MemoryFactService
from yuki.persistence.models import (
    MemoryFactModel,
)


@asynccontextmanager
async def other_writer_and_read_only(database):
    """Fail at the actual SQL boundary instead of relying on timing thresholds."""
    statements = []

    def capture(_connection, _cursor, statement, *_args):
        statements.append(statement)
        sql = statement.lstrip().upper()
        assert not sql.startswith(("INSERT", "UPDATE", "DELETE", "REPLACE", "BEGIN IMMEDIATE"))

    async with database.immediate_session():
        event.listen(database.engine.sync_engine, "before_cursor_execute", capture)
        try:
            yield statements
        finally:
            event.remove(database.engine.sync_engine, "before_cursor_execute", capture)


async def facts(database, count=1):
    repository = MemoryFactRepository(database)
    service = MemoryFactService(repository)
    rows = []
    for index in range(count):
        rows.append(
            await service.remember(
                MemoryFactCreate(
                    scope_type="self",
                    visibility_type="global",
                    category="test",
                    memory_key=f"writer:{index}",
                    content=f"writer fact {index}",
                    source_type="explicit",
                )
            )
        )
    return repository, service, rows


async def test_unexpired_and_empty_maintenance_do_not_acquire_writer(database):
    _repository, service, rows = await facts(database)
    worker = MemoryMaintenanceWorker(settings=make_settings(database.url), facts=service)
    async with other_writer_and_read_only(database):
        assert await worker.process_once() == 0
    async with database.immediate_session() as session:
        await session.execute(delete(MemoryFactModel).where(MemoryFactModel.id == rows[0].id))
    async with other_writer_and_read_only(database):
        assert await worker.process_once() == 0


async def test_missing_preference_returns_without_waiting_for_writer(database):
    import asyncio
    _, service, _ = await facts(database)
    async with other_writer_and_read_only(database):
        assert not await asyncio.wait_for(service.delete_preference("1001", "missing-key"), 1)
