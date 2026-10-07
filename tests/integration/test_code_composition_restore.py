"""Restore hands an open code composition to its owner before generic pairing."""

import json

from tests.support.work_session import WorkSession
from tests.unit.test_work_effect_results import owned_session

from qq_ai_bot.domain.messages import ChatMessage, ToolCall, ToolFunction
from qq_ai_bot.runtime.work_session import PendingComposition
from qq_ai_bot.services.turn_transcript import TurnTranscript

LATEST = (ChatMessage("system", "fixed"), ChatMessage("user", "current public chat"))


async def saved_code_call(database, tmp_path, *, version=1):
    control, first, _store = await owned_session(database, tmp_path)
    control.session = first
    call = ToolCall("code-1", ToolFunction("execute_code", '{"code":"x = 1"}'))
    first.transcript.append(ChatMessage("assistant", tool_calls=(call,)))
    await first.save("response", (call,))
    key = first.call_key(call.id)
    assert await control.repository.prepare_effect(
        control.lease,
        control.current["id"],
        key,
        "code_composition",
        composition={"version": version, "snapshot_revision": 3, "snapshot_ref": "abc"},
    )
    return control, first, call, key


def results_for(transcript, call_id):
    return [m for m in transcript.request().messages if m.tool_call_id == call_id]


async def test_open_composition_is_returned_to_owner_and_not_paired(database, tmp_path):
    control, first, call, key = await saved_code_call(database, tmp_path)
    resumed = WorkSession(control, first.contract)
    control.session = resumed
    restored = await resumed.restore(TurnTranscript(LATEST))
    assert resumed.pending_compositions == [
        # P05: the saved outer call travels with it so the owner resumes the
        # same composition without reading the transcript.
        PendingComposition(
            call_id=call.id,
            operation_id=key,
            snapshot_revision=3,
            snapshot_ref="abc",
            name=call.function.name,
            arguments=call.function.arguments,
        )
    ]
    # The original outer call keeps its response; no synthetic unknown result
    # and no chain retirement may happen before the program is resumed.
    assert resumed.uses_recovery_transcript
    assert results_for(restored, call.id) == []
    assert any(m.tool_calls for m in restored.request().messages)


async def test_settled_composition_pairs_once_from_its_receipt(database, tmp_path):
    control, first, _call, key = await saved_code_call(database, tmp_path)
    partial = json.dumps({"ok": False, "status": "partial", "replay_forbidden": True})
    await control.repository.record_effect(key, "accepted", {"result": partial})
    resumed = WorkSession(control, first.contract)
    control.session = resumed
    await resumed.restore(TurnTranscript(LATEST))
    assert resumed.pending_compositions == []
    assert await resumed.journal.effect_result(key) == partial


async def test_unrecognized_composition_version_keeps_conservative_path(database, tmp_path):
    control, first, _call, key = await saved_code_call(database, tmp_path, version=2)
    resumed = WorkSession(control, first.contract)
    control.session = resumed
    await resumed.restore(TurnTranscript(LATEST))
    assert resumed.pending_compositions == []
    assert json.loads(await resumed.journal.effect_result(key))["uncertain"] is True
