"""Retired task metadata cannot rewrite a paid call or its original receipt."""

import json
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import update
from tests.support.work_session import WorkSession, invoke_tool
from tests.unit.test_work_journal_source_retry import _saved, _session

from yuki.domain.messages import ChatMessage, ProviderContinuation, ToolCall, ToolFunction
from yuki.runtime.work_schema_v1 import work
from yuki.services.turn_transcript import TurnTranscript


@pytest.mark.parametrize("executed", [False, True])
async def test_original_reporting_call_recovers_without_rewriting_paid_input(database, executed):
    control, first, _selected, _unselected = await _session(database)
    call = ToolCall(
        "original-reporting-call",
        ToolFunction("task_control", '{"action":"update","reporting":"quiet"}'),
    )
    first.transcript.append(ChatMessage("assistant", tool_calls=(call,)))
    first.transcript.accept(
        ProviderContinuation(
            "openai",
            "responses",
            (
                {
                    "type": "function_call",
                    "call_id": call.id,
                    "name": call.function.name,
                    "arguments": call.function.arguments,
                },
            ),
        )
    )
    await first.save("response", (call,))
    original = await _saved(database, control)
    before = await control.repository.get(control.current["id"])
    original_receipt = json.dumps(
        {
            "ok": True,
            "work_id": control.current["id"],
            "state": "running",
            "revision": control.current["revision"],
            "ending_proposed": None,
        }
    )
    if executed:
        await invoke_tool(first, call, AsyncMock(return_value=original_receipt))

    resumed = WorkSession(control, first.contract)
    restored = await resumed.restore(TurnTranscript((ChatMessage("user", "today"),)))
    if executed:
        first.transcript.append_result(call.id, original_receipt)
    assert restored.request() == first.transcript.request()
    assert resumed.call_key(call.id) == first.call_key(call.id)

    async def execute_original():
        return await control.execute(
            call.function.name, json.loads(call.function.arguments), resumed.call_key(call.id)
        )

    execute = AsyncMock(side_effect=execute_original)
    result = await invoke_tool(resumed, call, execute)
    if executed:
        assert result == original_receipt
        execute.assert_not_awaited()
    else:
        assert json.loads(result)["ok"]
        execute.assert_awaited_once()
    saved = await _saved(database, control)
    if executed:
        assert saved["phase"] == "paired"
        for key in ("work_id", "chain_id", "contract"):
            assert saved[key] == original[key]
    else:
        assert saved == original
    after = await control.repository.get(control.current["id"])
    for key in ("id", "goal", "state", "checkpoint_json", "model_requests", "tool_calls"):
        assert after[key] == before[key]


@pytest.mark.parametrize("payload", [None, {"transcript_ref": "original"}])
async def test_normal_checkpoint_drops_retired_metadata_and_keeps_owned_facts(database, payload):
    control, _session_owner, _selected, _unselected = await _session(database)
    old = {
        "communication": {"reporting": "quiet"},
        "context_note": {"revision": 1, "payload": {"text": "tell me when it is done"}},
        "accepted_control": {"action": "need_input", "reason": "which file?"},
    }
    async with database.immediate_session() as writer:
        await writer.execute(
            update(work)
            .where(work.c.id == control.current["id"])
            .values(checkpoint_json=json.dumps(old))
        )
    before = await control.repository.get(control.current["id"])
    await control.repository.checkpoint(control.lease, control.current["id"], payload, models=1)
    after = await control.repository.get(control.current["id"])
    checkpoint = json.loads(after["checkpoint_json"])
    assert checkpoint == {
        **(payload or {}),
        **{k: old[k] for k in ("context_note", "accepted_control")},
    }
    assert after["model_requests"] == before["model_requests"] + 1
    assert after["state"] == before["state"]
