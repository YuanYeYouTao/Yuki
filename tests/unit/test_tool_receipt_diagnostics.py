"""Only original tool evidence writes; closing child facts need no duplicate row."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import event, select
from tests.support.social_identity_cases import social_env

from yuki.codemode.api_projection import ToolReceiptView
from yuki.codemode.contract import STOP_UNKNOWN_EFFECT
from yuki.codemode.driver import (
    ChildClass,
    CodeCompositionYield,
    CodeModeDriver,
    _Child,
    _State,
    _Stop,
)
from yuki.execution_trace.db_models import ExecutionTraceEntryModel
from yuki.execution_trace.payload import decode_payload
from yuki.execution_trace.recorder import TraceRecorder, trace_span
from yuki.persistence.models import ChatEventModel, MemoryToolReceiptModel
from yuki.tool_results import recorder as receipt_module
from yuki.tool_results.recorder import ToolReceiptRepository


async def test_tool_evidence_retains_one_original_receipt_and_one_write_commit(database, tmp_path):
    env = await social_env(database, tmp_path)
    async with database.sessions() as reader:
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
    async with database.sessions() as reader:
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


@pytest.mark.parametrize("outcome", ["success", "stop", "yield", "cancel"])
async def test_child_outcome_survives_success_and_host_closing_error_once(
    database, monkeypatch, outcome
):
    driver = CodeModeDriver(SimpleNamespace(control=None), None)
    pending = outcome == "yield"
    uncertain = outcome in {"stop", "cancel"}
    status = "pending" if pending else "unknown" if uncertain else "succeeded"
    child = _Child(
        "original-child",
        1,
        1,
        0,
        "terminal_exec",
        "{}",
        ChildClass("write", False, True),
        view=ToolReceiptView(
            status=status,
            ok=outcome == "success",
            data=None,
            error=None,
            operation_id="original-child",
            result_ref=None,
            executed=True,
            reused=False,
            pending=pending,
            uncertain=uncertain,
            complete=outcome == "success",
        ),
    )
    failure = {
        "success": None,
        "stop": _Stop(STOP_UNKNOWN_EFFECT, "original-child"),
        "yield": CodeCompositionYield(),
        "cancel": asyncio.CancelledError(),
    }[outcome]
    monkeypatch.setattr(driver, "_dispatch_child", AsyncMock(side_effect=failure))
    traces = TraceRecorder(database)
    async with trace_span("test", {}, recorder=traces, record_start=False):
        if failure is None:
            await driver._dispatch_one(_State("parent", 1), child)
        else:
            with pytest.raises(type(failure)):
                await driver._dispatch_one(_State("parent", 1), child)
    async with database.sessions() as reader:
        rows = list(
            await reader.scalars(
                select(ExecutionTraceEntryModel)
                .where(ExecutionTraceEntryModel.kind.like("code_child%"))
                .order_by(ExecutionTraceEntryModel.id)
            )
        )
    assert [row.kind for row in rows] == [
        "code_child_start",
        "code_child_end" if failure is None else "code_child_error",
    ]
    final = rows[-1]
    payload = decode_payload(
        final.payload_gzip, size=final.payload_bytes, digest=final.payload_sha256
    )["data"]
    assert payload["result"] == {
        "operation_id": "original-child",
        "status": status,
        "executed": True,
        "reused": False,
        "pending": pending,
        "uncertain": uncertain,
    }
