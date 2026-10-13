"""Manager failures preserve failed and unknown tool effect receipts."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from tests.conftest import make_settings

from qq_ai_bot.capabilities.invocation import ToolInvocationContext, current_invocation
from qq_ai_bot.capabilities.models import CapabilityEffect
from qq_ai_bot.capabilities.results import resolve_mutation_commit
from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.domain.messages import InboundMessage, SenderIdentity
from qq_ai_bot.memory.repository import MemoryFactRepository
from qq_ai_bot.memory.service import MemoryFactService
from qq_ai_bot.persistence.repositories import EventLedgerRepository
from qq_ai_bot.sandbox.client import SandboxClient
from qq_ai_bot.services.agent_tools import AgentToolService, ToolRuntime


@pytest.mark.parametrize(
    ("tool", "payload", "committed"),
    [
        ("workspace_write", {"error": "version_conflict"}, False),
        ("workspace_patch", {"error": "workspace_full"}, False),
        ("terminal_exec", {"error": "environment_busy"}, False),
        ("terminal_write", {"error": "idempotency_conflict"}, False),
        ("workspace_move", {"error": "sandbox_unavailable", "uncertain": True}, None),
        ("terminal_exec", {"error": "sandbox_submission_unknown", "uncertain": True}, None),
        ("workspace_write", {"path": "result.txt", "version": "new"}, True),
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
        gateway=None,
        allow_generic_onebot=False,
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
        ("workspace_write", True),
        ("workspace_upload", True),
        ("terminal_write", True),
        ("workspace_read", False),
        ("terminal_read", False),
    ],
)
async def test_lost_manager_response_preserves_unknown_effect(monkeypatch, tool, uncertain):
    reader = SimpleNamespace(readline=AsyncMock(side_effect=OSError("response lost")))
    writer = SimpleNamespace(
        write=Mock(),
        drain=AsyncMock(),
        close=Mock(),
        wait_closed=AsyncMock(),
    )
    monkeypatch.setattr(
        "asyncio.open_unix_connection",
        AsyncMock(return_value=(reader, writer)),
    )
    result = await SandboxClient(Path("/unused.sock")).execute(
        tool,
        {},
        request_id="original-request",
    )
    assert result["error"] == "sandbox_unavailable"
    assert result["uncertain"] is uncertain
    writer.write.assert_called_once()
    writer.close.assert_called_once()


async def test_connection_failure_has_no_dispatched_effect(monkeypatch):
    monkeypatch.setattr("asyncio.open_unix_connection", AsyncMock(side_effect=OSError("offline")))
    result = await SandboxClient(Path("/unused.sock")).execute(
        "workspace_write",
        {},
        request_id="original-request",
    )
    assert result["error"] == "sandbox_unavailable"
    assert result["uncertain"] is False
