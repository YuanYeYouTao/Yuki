"""Manager failures preserve failed and unknown tool effect receipts."""

import asyncio
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy.exc import OperationalError
from tests.conftest import make_settings

from yuki.capabilities.invocation import ToolInvocationContext, current_invocation
from yuki.capabilities.models import CapabilityEffect
from yuki.capabilities.results import resolve_mutation_commit
from yuki.domain.conversations import ScopeType
from yuki.domain.messages import InboundMessage, SenderIdentity
from yuki.memory.repository import MemoryFactRepository
from yuki.memory.service import MemoryFactService
from yuki.persistence.repositories import EventLedgerRepository
from yuki.sandbox.client import SandboxClient
from yuki.services.agent_tools import AgentToolService, ToolRuntime


@pytest.mark.parametrize(
    ("notes", "committed", "retryable"),
    [
        (("database_write_phase:begin", "database_rollback:confirmed"), False, True),
        (("database_write_phase:flush", "database_rollback:confirmed"), False, True),
        (("database_write_phase:commit", "database_rollback:confirmed"), None, False),
        (("database_write_phase:commit", "database_commit:confirmed"), True, False),
        ((), None, False),
    ],
)
async def test_database_tool_failure_keeps_actual_commit_and_original_phase(
    database, notes, committed, retryable
):
    native = sqlite3.OperationalError("database is locked")
    native.sqlite_errorcode = sqlite3.SQLITE_BUSY
    failure = OperationalError("original write", {}, native)
    for note in notes:
        failure.add_note(note)
    tools = AgentToolService(
        settings=make_settings(database.url),
        ledger=EventLedgerRepository(database),
        memories=MemoryFactService(MemoryFactRepository(database)),
    )
    tools.sandbox_client = SimpleNamespace(execute=AsyncMock(side_effect=failure))
    tools.workspace_service = tools.sandbox_client
    runtime = ToolRuntime(
        inbound=InboundMessage(
            "original-trigger",
            "message:private:normal",
            ScopeType.PRIVATE,
            SenderIdentity("1001"),
            "write a file",
            bot_user_id="8000",
        ),
        execution_id="original-execution",
        conversation_id="original-conversation",
        target_presence_id="original-presence",
    )
    token = current_invocation.set(ToolInvocationContext(runtime, "original-call"))
    try:
        result = await tools.execute("terminal_exec", "{}", runtime)
    finally:
        current_invocation.reset(token)
    assert not result.ok and result.error_code == "database_failure"
    assert result.mutation_committed is committed
    assert result.uncertain is (committed is None)
    assert result.retryable is retryable
    expected_phase = next((note.split(":", 1)[1] for note in notes if "write_phase:" in note), None)
    assert result.metadata == {"database_write_phase": expected_phase}
    assert (
        resolve_mutation_commit(result, SimpleNamespace(effect=CapabilityEffect.WRITE_STATE))
        is committed
    )
    tools.sandbox_client.execute.assert_awaited_once()


@pytest.mark.parametrize(
    ("tool", "payload", "committed"),
    [
        (
            "save_conversation_attachment_to_workspace",
            {"error": "attachment_source_changed"},
            False,
        ),
        ("save_conversation_attachment_to_workspace", {"error": "attachment_expired"}, False),
        ("terminal_exec", {"error": "environment_unavailable"}, False),
        ("terminal_control", {"error": "idempotency_conflict"}, False),
        (
            "save_conversation_attachment_to_workspace",
            {"error": "sandbox_unavailable", "uncertain": True},
            None,
        ),
        ("terminal_exec", {"error": "sandbox_submission_unknown", "uncertain": True}, None),
        ("save_conversation_attachment_to_workspace", {"path": "saved.txt", "saved": True}, True),
    ],
)
async def test_tool_operation_receipt(database, tool, payload, committed):
    tools = AgentToolService(
        settings=make_settings(database.url),
        ledger=EventLedgerRepository(database),
        memories=MemoryFactService(MemoryFactRepository(database)),
    )
    backend = SimpleNamespace(execute=AsyncMock(return_value=payload))
    tools.workspace_service = backend
    tools.sandbox_client = backend
    runtime = ToolRuntime(
        inbound=InboundMessage(
            "trigger",
            "message:private:normal",
            ScopeType.PRIVATE,
            SenderIdentity("1001"),
            "write a file",
            bot_user_id="8000",
        ),
        execution_id="execution-test",
        conversation_id="conversation-test",
        target_presence_id="presence-test",
    )
    token = current_invocation.set(ToolInvocationContext(runtime, "original-call"))
    try:
        result = await tools.execute(tool, "{}", runtime)
    finally:
        current_invocation.reset(token)
    assert result.ok is ("error" not in payload)
    assert result.error_code == payload.get("error")
    assert result.data == payload
    assert result.uncertain is bool(payload.get("uncertain"))
    descriptor = SimpleNamespace(effect=CapabilityEffect.WRITE_STATE)
    assert resolve_mutation_commit(result, descriptor) is committed
    backend.execute.assert_awaited_once()


@pytest.mark.parametrize(
    ("tool", "uncertain"),
    [
        ("workspace_publish", True),
        ("terminal_control", True),
        ("workspace_media_read", False),
        ("terminal_read", False),
    ],
)
async def test_lost_manager_response_preserves_unknown_effect(monkeypatch, tool, uncertain):
    reader = asyncio.StreamReader()
    reader.set_exception(OSError("response lost"))
    writer = SimpleNamespace(
        write=Mock(),
        drain=AsyncMock(),
        close=Mock(),
        wait_closed=AsyncMock(),
    )
    monkeypatch.setattr(
        "asyncio.open_unix_connection",
        AsyncMock(return_value=(reader, writer)),
        raising=False,
    )
    result = await SandboxClient(Path("/unused.sock")).execute(
        tool,
        {},
        request_id="original-request",
    )
    assert result["error"] == "sandbox_unavailable"
    assert result["uncertain"] is uncertain
    writer.write.assert_called_once()
    assert json.loads(writer.write.call_args.args[0]) == {
        "method": tool,
        "args": {},
        "request_id": "original-request",
    }
    writer.drain.assert_awaited_once()
    writer.close.assert_called_once()
    writer.wait_closed.assert_awaited_once()


async def test_connection_failure_has_no_dispatched_effect(monkeypatch):
    monkeypatch.setattr(
        "asyncio.open_unix_connection", AsyncMock(side_effect=OSError("offline")), raising=False
    )
    result = await SandboxClient(Path("/unused.sock")).execute(
        "workspace_publish",
        {},
        request_id="original-request",
    )
    assert result["error"] == "sandbox_unavailable"
    assert result["uncertain"] is False


@pytest.mark.parametrize("tool", ["terminal_exec", "save_conversation_attachment_to_workspace"])
@pytest.mark.parametrize("failure_type", [OSError, RuntimeError, ValueError])
@pytest.mark.parametrize(
    "phase,confirmed,committed",
    [("commit", False, None), ("cleanup", True, True), ("cleanup", False, None)],
)
async def test_non_sqlalchemy_commit_failure_keeps_real_certainty(
    database, tool, failure_type, phase, confirmed, committed
):
    failure = failure_type("original phase failure")
    failure.add_note(f"database_write_phase:{phase}")
    if confirmed:
        failure.add_note("database_commit:confirmed")
    tools = AgentToolService(
        settings=make_settings(database.url),
        ledger=EventLedgerRepository(database),
        memories=MemoryFactService(MemoryFactRepository(database)),
    )
    backend = SimpleNamespace(execute=AsyncMock(side_effect=failure))
    tools.sandbox_client = tools.workspace_service = backend
    runtime = ToolRuntime(
        inbound=InboundMessage(
            "original-trigger",
            "message:private:normal",
            ScopeType.PRIVATE,
            SenderIdentity("1001"),
            "write a file",
            bot_user_id="8000",
        ),
        execution_id="original-execution",
        conversation_id="original-conversation",
        target_presence_id="original-presence",
    )
    token = current_invocation.set(ToolInvocationContext(runtime, "original-call"))
    try:
        result = await tools.execute(tool, "{}", runtime)
    finally:
        current_invocation.reset(token)
    assert not result.ok and result.error_code == "database_failure"
    assert result.mutation_committed is committed and result.uncertain is (committed is None)
    assert result.metadata == {"database_write_phase": phase} and not result.retryable
    backend.execute.assert_awaited_once()


async def test_rolled_back_business_value_error_remains_business_error(database):
    failure = ValueError("bad selected attachment")
    failure.add_note("database_write_phase:execute")
    failure.add_note("database_rollback:confirmed")
    tools = AgentToolService(
        settings=make_settings(database.url),
        ledger=EventLedgerRepository(database),
        memories=MemoryFactService(MemoryFactRepository(database)),
    )
    tools.workspace_service = SimpleNamespace(execute=AsyncMock(side_effect=failure))
    result = await tools.execute(
        "save_conversation_attachment_to_workspace",
        "{}",
        ToolRuntime(
            inbound=InboundMessage(
                "trigger",
                "message:private:normal",
                ScopeType.PRIVATE,
                SenderIdentity("1001"),
                "file",
                bot_user_id="8000",
            )
        ),
    )
    assert (
        result.error_code == "ValueError"
        and result.mutation_committed is False
        and not result.uncertain
    )
