"""Readonly receipt identity is opaque after the Host chain/sequence prefix."""

import json

import pytest
from sqlalchemy import select, update
from tests.support.work_session import WorkSession, invoke_tool
from tests.unit.test_work_readonly_reuse import _call, _setup

from qq_ai_bot.domain.messages import ChatMessage
from qq_ai_bot.runtime.work_journal import JournalUnavailable
from qq_ai_bot.runtime.work_schema_v1 import effects
from qq_ai_bot.services.turn_transcript import TurnTranscript


@pytest.mark.parametrize(
    "original_id,alias_id",
    [
        ("original-" + "x" * 1200, "alias-" + "y" * 1300),
        ("provider:response:call:original", "provider:response:call:alias"),
        ("provider:response:" + "x" * 1200, "provider:response:" + "y" * 1300),
        ("provider:original", "alias-" + "y" * 1300),
        ("original-" + "x" * 1200, "provider:alias"),
    ],
    ids=["long_ids", "colon_ids", "long_colon_ids", "short_to_long", "long_to_short"],
)
async def test_original_opaque_readonly_call_restores_alias_without_reexecution(
    database, tmp_path, original_id, alias_id
):
    control, first, _, _ = await _setup(database, tmp_path)
    original_call, alias = _call(original_id), _call(alias_id)
    invocations = []

    async def invoke():
        invocations.append(original_id)
        return json.dumps({"ok": True, "selected": "original evidence"})

    receipt = await invoke_tool(first, original_call, invoke, side_effecting=False)
    original_key = first.call_key(original_id)
    first.sequence += 1
    first.transcript.append(ChatMessage("assistant", tool_calls=(alias,)))
    first.pending_readonly_keys = {alias_id: original_key}
    await first.save("response", (alias,))
    current = await control.repository.get(control.current["id"])

    resumed = WorkSession(control, first.contract)
    restored = await resumed.restore(TurnTranscript(()))
    results = [message for message in restored.request().messages if message.role == "tool"]
    assert [(message.tool_call_id, message.content) for message in results] == [(alias_id, receipt)]
    assert invocations == [original_id]
    assert await resumed.journal.effect_state(original_key) == "accepted"
    assert resumed.sequence == first.sequence
    observed = await control.repository.get(control.current["id"])
    assert observed["id"] == current["id"]
    assert observed["model_requests"] == current["model_requests"]
    assert observed["tool_calls"] == current["tool_calls"]
    await control.repository.release(control.lease)


@pytest.mark.parametrize(
    "violation", ["missing", "owner", "chain", "future", "provider", "operation", "child"]
)
async def test_hashed_readonly_key_requires_original_persisted_invocation(
    database, tmp_path, violation
):
    control, first, _, _ = await _setup(database, tmp_path)
    original_call, alias = _call("original-" + "x" * 1200), _call("alias")

    async def invoke():
        return '{"ok":true}'

    await invoke_tool(first, original_call, invoke, side_effecting=False)
    key = first.call_key(original_call.id)
    assert key.startswith("invocation:v1:")
    async with database.sessions() as writer, writer.begin():
        receipt = json.loads(
            await writer.scalar(select(effects.c.receipt_json).where(effects.c.effect_key == key))
        )
        if violation == "missing":
            receipt.pop("invocation")
        else:
            field, value = {
                "owner": ("owner_execution_id", "other-work"),
                "chain": ("chain_id", "other-chain"),
                "future": ("request_sequence", first.sequence + 1),
                "provider": ("provider_call_id", "different-provider-call"),
                "operation": ("operation_id", "different-operation"),
                "child": ("parent_effect_key", "another-parent"),
            }[violation]
            receipt["invocation"][field] = value
        await writer.execute(
            update(effects)
            .where(effects.c.effect_key == key)
            .values(receipt_json=json.dumps(receipt))
        )
    first.transcript.append(ChatMessage("assistant", tool_calls=(alias,)))
    first.pending_readonly_keys = {alias.id: key}
    await first.save("response", (alias,))
    with pytest.raises(JournalUnavailable, match="work_readonly_reuse_corrupt"):
        await WorkSession(control, first.contract).restore(TurnTranscript(()))
    await control.repository.release(control.lease)


@pytest.mark.parametrize("violation", ["other_chain", "future_sequence", "empty_identity"])
async def test_opaque_call_suffix_does_not_bypass_host_prefix(database, tmp_path, violation):
    control, first, _, _ = await _setup(database, tmp_path)
    original_call, alias = _call("provider:original"), _call("provider:alias")

    async def invoke():
        return '{"ok":true}'

    await invoke_tool(first, original_call, invoke, side_effecting=False)
    key = first.call_key(original_call.id)
    chain, sequence, identity = key.split(":", 2)
    if violation == "other_chain":
        key = f"other-chain:{sequence}:{identity}"
    elif violation == "future_sequence":
        key = f"{chain}:{int(sequence) + 1}:{identity}"
    else:
        key = f"{chain}:{sequence}:"
    first.transcript.append(ChatMessage("assistant", tool_calls=(alias,)))
    first.pending_readonly_keys = {alias.id: key}
    await first.save("response", (alias,))
    with pytest.raises(JournalUnavailable, match="work_readonly_reuse_corrupt"):
        await WorkSession(control, first.contract).restore(TurnTranscript(()))
    await control.repository.release(control.lease)
