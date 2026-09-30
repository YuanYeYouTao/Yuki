"""Control memory mutations retain one receipt across a fresh SQLite snapshot."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import event, func, select, update
from tests.unit.test_control_plane_foundation import context

from qq_ai_bot.control_plane import ControlCommand, ControlCommandService
from qq_ai_bot.conversation.canonical_db_models import ControlCommandReceiptModel
from qq_ai_bot.memory.models import MemoryFactCreate
from qq_ai_bot.memory.repository import MemoryFactRepository
from qq_ai_bot.memory.service import MemoryFactService
from qq_ai_bot.persistence.control_command import ControlCommandAdapter
from qq_ai_bot.persistence.models import AdminOperationEventModel, MemoryFactModel
from qq_ai_bot.persistence.unit_of_work import state_revision


@pytest.mark.parametrize(
    "action,race", [("confirm", False), ("quarantine", False), ("quarantine", True)]
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
        if race and first and len(prepared) == 1:
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
    assert prepared == [(rows[0].id,)] * (2 if race else 1)
    assert historical_reads_after_write == []
    assert native_errors == ([517] if race else [])
    async with database.sessions() as session:
        assert await session.scalar(select(func.count(ControlCommandReceiptModel.id))) == 1
        assert await session.scalar(select(func.count(AdminOperationEventModel.id))) == 1
    current = await facts.get_fact(rows[0].id)
    assert current.evidence_count == rows[0].evidence_count
    if action == "confirm":
        assert current.last_confirmed_at is not None
    else:
        assert current.review_state.value == "quarantined"
