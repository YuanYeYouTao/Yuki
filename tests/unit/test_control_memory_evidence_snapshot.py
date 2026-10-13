"""Control memory mutations retain one receipt across a fresh SQLite snapshot."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import event, func, select, update
from tests.support.control_plane_foundation_helpers import context

from qq_ai_bot.control_plane import ControlCommand, ControlCommandService
from qq_ai_bot.conversation.canonical_db_models import ControlCommandReceiptModel
from qq_ai_bot.memory.models import MemoryFactCreate
from qq_ai_bot.memory.repository import MemoryFactRepository
from qq_ai_bot.memory.service import MemoryFactService
from qq_ai_bot.persistence.control_command import ControlCommandAdapter
from qq_ai_bot.persistence.models import AdminOperationEventModel, MemoryFactModel
from qq_ai_bot.persistence.unit_of_work import state_revision


@pytest.mark.parametrize(
    "action,race", [("confirm", 0), ("quarantine", 0), ("quarantine", 1), ("quarantine", 4)]
)
async def test_control_memory_prepares_before_writer_and_retries_original_receipt(
    database, monkeypatch, action, race
):
    facts = MemoryFactService(MemoryFactRepository(database))
    rows = []
    for index in range(2):
        rows.append(
            await facts.remember(
                MemoryFactCreate(
                    scope_type="self",
                    visibility_type="global",
                    category="test",
                    memory_key=f"control-snapshot:{index}",
                    content=f"control fact {index}",
                    source_type="explicit",
                )
            )
        )
    prepared = []
    prepare = facts.prepare_evidence_write

    async def capture(fact_ids, *, session, targets=()):
        first = not session.info.get("test_control_prepared")
        if first:
            assert not session.info.get("memory_evidence_write_started")
            session.info["test_control_prepared"] = True
            prepared.append(fact_ids)
        await prepare(fact_ids, session=session, targets=targets)
        if first and len(prepared) <= race:
            # A different WAL writer commits while the command is still reading.
            async with database.immediate_session() as writer:
                await writer.execute(
                    update(MemoryFactModel)
                    .where(MemoryFactModel.id == rows[1].id)
                    .values(updated_at=datetime.now(UTC))
                )

    monkeypatch.setattr(facts, "prepare_evidence_write", capture)
    historical_reads_after_write = []
    writing = set()
    native_errors = []

    def trace(connection, _cursor, statement, *_args):
        sql = statement.lstrip().upper()
        if sql.startswith(("INSERT", "UPDATE", "DELETE")):
            writing.add(connection)
        elif connection in writing and sql.startswith("SELECT") and "MEMORY_EVIDENCE" in sql:
            historical_reads_after_write.append(statement)

    def reset(connection):
        writing.discard(connection)

    def handle_error(error_context):
        native_errors.append(getattr(error_context.original_exception, "sqlite_errorcode", None))

    engine = database.engine.sync_engine
    event.listen(engine, "before_cursor_execute", trace)
    event.listen(engine, "commit", reset)
    event.listen(engine, "rollback", reset)
    event.listen(engine, "handle_error", handle_error)
    try:
        commands = ControlCommandService(ControlCommandAdapter(database, memories=facts))
        ctx = context("control.memory.mutate")
        command = ControlCommand(
            request_id=ctx.request_id,
            expected_revision=state_revision(rows[0].updated_at),
            payload={"action": action, "resource_id": str(rows[0].id)},
        )
        result = await commands.mutate_memory(ctx, command)
        assert result.success
        assert await commands.mutate_memory(ctx, command) == result
    finally:
        event.remove(engine, "before_cursor_execute", trace)
        event.remove(engine, "commit", reset)
        event.remove(engine, "rollback", reset)
        event.remove(engine, "handle_error", handle_error)
    assert prepared == [(rows[0].id,)] * (race + 1)
    assert historical_reads_after_write == []
    assert native_errors == [517] * race
    async with database.sessions() as session:
        assert await session.scalar(select(func.count(ControlCommandReceiptModel.id))) == 1
        assert await session.scalar(select(func.count(AdminOperationEventModel.id))) == 1
    current = await facts.get_fact(rows[0].id)
    assert current.evidence_count == rows[0].evidence_count
    if action == "confirm":
        assert current.last_confirmed_at is not None
    else:
        assert current.review_state.value == "quarantined"


async def test_control_memory_unknown_commit_acknowledgement_does_not_retry(database, monkeypatch):
    import sqlite3
    from contextlib import asynccontextmanager

    from sqlalchemy.exc import OperationalError

    facts = MemoryFactService(MemoryFactRepository(database))
    fact = await facts.remember(
        MemoryFactCreate(
            scope_type="self",
            visibility_type="global",
            category="test",
            memory_key="control:lost-ack",
            content="retained fact",
            source_type="explicit",
        )
    )
    transaction = facts.repository.transaction
    native = sqlite3.OperationalError("commit acknowledgement lost")
    native.sqlite_errorcode = 517
    failure = OperationalError("COMMIT", {}, native)
    writes = 0

    @asynccontextmanager
    async def lost_confirmation(*, read_snapshot=False):
        nonlocal writes
        async with transaction(read_snapshot=read_snapshot) as session:
            yield session
            changed = session.info.get("memory_evidence_write_started")
        if changed:
            writes += 1
            raise failure

    commands = ControlCommandService(ControlCommandAdapter(database, memories=facts))
    ctx = context("control.memory.mutate")
    command = ControlCommand(
        request_id=ctx.request_id,
        expected_revision=state_revision(fact.updated_at),
        payload={"action": "quarantine", "resource_id": str(fact.id)},
    )
    monkeypatch.setattr(facts.repository, "transaction", lost_confirmation)
    with pytest.raises(OperationalError) as error:
        await commands.mutate_memory(ctx, command)
    assert error.value is failure and writes == 1
    assert (await facts.get_fact(fact.id)).review_state.value == "quarantined"
    async with database.sessions() as reader:
        assert await reader.scalar(select(func.count(ControlCommandReceiptModel.id))) == 1
        assert await reader.scalar(select(func.count(AdminOperationEventModel.id))) == 1
    monkeypatch.setattr(facts.repository, "transaction", transaction)
    assert (await commands.mutate_memory(ctx, command)).success
    assert writes == 1
