"""Real WAL empty maintenance reads, fair source windows, and recovery CAS."""

from contextlib import asynccontextmanager

from sqlalchemy import delete, event
from tests.conftest import make_settings

from qq_ai_bot.memory.maintenance import MemoryMaintenanceWorker
from qq_ai_bot.memory.models import MemoryFactCreate
from qq_ai_bot.memory.repository import MemoryFactRepository
from qq_ai_bot.memory.service import MemoryFactService
from qq_ai_bot.persistence.models import (
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
    repository, service, rows = await facts(database)
    worker = MemoryMaintenanceWorker(settings=make_settings(database.url), facts=service)
    async with other_writer_and_read_only(database):
        assert await worker.process_once() == 0
    async with repository.transaction() as session:
        await session.execute(delete(MemoryFactModel).where(MemoryFactModel.id == rows[0].id))
    async with other_writer_and_read_only(database):
        assert await worker.process_once() == 0
