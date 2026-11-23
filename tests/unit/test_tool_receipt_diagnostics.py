"""Only original tool evidence writes a receipt."""

from sqlalchemy import event, select
from tests.support.social_identity_cases import social_env

from yuki.persistence.models import ChatEventModel, MemoryToolReceiptModel
from yuki.tool_results import recorder as receipt_module
from yuki.tool_results.recorder import ToolReceiptRepository


async def test_tool_evidence_retains_one_original_receipt_and_one_write_commit(database, tmp_path):
    env = await social_env(database, tmp_path)
    async with database.read_session() as reader:
        source = await reader.scalar(select(ChatEventModel))
    statements, commits = [], []

    def before_sql(_conn, _cursor, sql, _parameters, _context, _many):
        statements.append(sql)

    def committing(_conn):
        commits.append(True)

    event.listen(database.engine.sync_engine, "before_cursor_execute", before_sql)
    event.listen(database.engine.sync_engine, "commit", committing)
    try:
        await ToolReceiptRepository(database).record_receipt(
            provider_id="core",
            tool_name="terminal_exec",
            success=True,
            error_category=None,
            result_excerpt="original verified result",
            trigger_event_id=source.id,
            bot_user_id=env.bot.self_id,
            canonical_conversation_id=source.canonical_conversation_id,
            tool_call_id="original-call",
            execution_id="original-execution",
        )
    finally:
        event.remove(database.engine.sync_engine, "before_cursor_execute", before_sql)
        event.remove(database.engine.sync_engine, "commit", committing)
    writes = [sql for sql in statements if sql.lstrip().upper().startswith("INSERT")]
    assert len(writes) == len(commits) == 1
    assert "memory_tool_receipts" in writes[0]
    async with database.read_session() as reader:
        receipt = await reader.scalar(select(MemoryToolReceiptModel))
        assert receipt.trigger_event_id == source.id
        assert receipt.canonical_space_id == env.space
        assert receipt.tool_call_id == "original-call"
        assert receipt.execution_id == "original-execution"
        assert receipt.result_excerpt == "original verified result"


async def test_without_original_evidence_source_no_stats_or_body_preparation_runs(
    database, monkeypatch
):
    statements = []

    def before_sql(_conn, _cursor, sql, _parameters, _context, _many):
        statements.append(sql)

    def unused_result(_value):
        raise AssertionError("no source means there is no receipt body to prepare")

    monkeypatch.setattr(receipt_module, "_redact_reflection_result", unused_result)
    event.listen(database.engine.sync_engine, "before_cursor_execute", before_sql)
    try:
        await ToolReceiptRepository(database).record_receipt(
            provider_id="core",
            tool_name="terminal_exec",
            success=True,
            error_category=None,
            result_excerpt="unused result",
        )
    finally:
        event.remove(database.engine.sync_engine, "before_cursor_execute", before_sql)
    assert statements == []
