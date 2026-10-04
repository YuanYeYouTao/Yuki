"""Original call identities survive equal arguments and out-of-order completion."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from qq_ai_bot.capabilities.coordinator import ToolInvocationCoordinator
from qq_ai_bot.capabilities.invocation import (
    Invocation,
    InvocationIdentity,
    TrustedInvocationContext,
    direct_invocations,
    direct_operation_id,
)
from qq_ai_bot.domain.messages import ToolCall, ToolFunction


class IdentityBackend:
    def __init__(self):
        self.started = []
        self.completed = []

    def parallel_safe(self, name, runtime):
        return name == "read"

    def is_side_effecting(self, name, arguments, runtime):
        return name != "read"

    async def execute_call(self, invocation):
        self.started.append(invocation)
        await asyncio.sleep(0.01 if invocation.call.id == "slow" else 0)
        self.completed.append(invocation.call.id)
        return json.dumps({"ok": True, "operation_id": invocation.identity.operation_id})


async def execute(calls, backend, *, sequence=1):
    return await ToolInvocationCoordinator().execute_batch(
        calls,
        backend,
        SimpleNamespace(execution_id="trusted-run", work_control=None),
        remaining_calls=10,
        max_parallel_calls=2,
        chain_id="chain",
        request_sequence=sequence,
    )


async def test_equal_argument_sends_have_two_original_operations():
    backend = IdentityBackend()
    calls = tuple(ToolCall(i, ToolFunction("send_message", '{"text":"same"}')) for i in ("a", "b"))
    result = await execute(calls, backend)
    assert result.executed_count == 2
    assert [i.identity.operation_id for i in backend.started] == ["chain:1:a", "chain:1:b"]


async def test_cross_response_call_id_reuse_is_distinct():
    backend = IdentityBackend()
    calls = (ToolCall("call_0", ToolFunction("send_message", "{}")),)
    await execute(calls, backend, sequence=1)
    await execute(calls, backend, sequence=2)
    assert [i.identity.operation_id for i in backend.started] == [
        "chain:1:call_0",
        "chain:2:call_0",
    ]


async def test_duplicate_response_ids_are_rejected_before_dispatch():
    backend = IdentityBackend()
    calls = (
        ToolCall("same", ToolFunction("send_message", "{}")),
        ToolCall("same", ToolFunction("send_message", '{"text":"different"}')),
    )
    result = await execute(calls, backend)
    assert not backend.started
    assert result.executed_count == 0
    assert all(
        json.loads(payload)["error"] == "duplicate_provider_call_id"
        for _, payload, _ in result.calls
    )


async def test_read_completion_order_does_not_change_identity_or_result_order():
    backend = IdentityBackend()
    calls = tuple(ToolCall(i, ToolFunction("read", "{}")) for i in ("slow", "fast"))
    result = await execute(calls, backend)
    assert backend.completed == ["fast", "slow"]
    assert [json.loads(payload)["operation_id"] for _, payload, _ in result.calls] == [
        "chain:1:slow",
        "chain:1:fast",
    ]


def test_original_work_journal_key_is_preserved():
    session = SimpleNamespace(transcript=SimpleNamespace(chain_id="original"), sequence=7)
    runtime = SimpleNamespace(work_control=SimpleNamespace(session=session, current={"id": "work"}))
    call = ToolCall("call_0", ToolFunction("read", "{}"))
    identity = direct_invocations((call,), runtime)[0].identity
    assert identity.operation_id == "original:7:call_0"
    assert identity.owner_execution_id == "work"


def test_identity_cannot_name_a_different_provider_call():
    with pytest.raises(ValueError, match="invocation_call_identity_conflict"):
        Invocation(
            InvocationIdentity("operation", "owner", "chain", 1, "a"),
            ToolCall("b", ToolFunction("read", "{}")),
            TrustedInvocationContext(object(), "manifest"),
        )


def test_long_operation_key_is_bounded_without_truncating_original_identity():
    first = direct_operation_id("chain", 1, "a" * 1000)
    second = direct_operation_id("chain", 1, "a" * 999 + "b")
    assert first != second and len(first.encode()) <= 256
    assert first == direct_operation_id("chain", 1, "a" * 1000)
