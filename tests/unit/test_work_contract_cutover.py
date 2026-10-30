"""A retired manifest leaves original facts and resumes the same Work on a new chain."""

import json

import pytest
from tests.support.work_session import WorkSession, invoke_tool
from tests.unit.test_work_protocol_continuity import _control

from yuki.domain.messages import ChatMessage, ProviderContinuation, ToolCall, ToolFunction
from yuki.runtime.work_journal import JournalUnavailable
from yuki.services.turn_transcript import TurnTranscript


@pytest.mark.parametrize("phase", ["dispatched", "delivery", "delivered"])
async def test_retired_checkpoint_resumes_original_work_without_old_protocol(
    database, tmp_path, phase
):
    control = await _control(database, tmp_path, worker=True)
    before = WorkSession(control, "retired-contract")
    await before.restore(TurnTranscript((ChatMessage("user", "retired private creation text"),)))
    before.transcript.accept(
        ProviderContinuation(
            provider="gemini",
            protocol="gemini",
            payload={
                "contents": [
                    {"role": "model", "parts": [{"thoughtSignature": "retired-signature"}]}
                ]
            },
        )
    )
    before.progress["delivery_plan"] = [{"text": "retired frozen body", "media": []}]
    await before.save(phase)
    await control.repository.checkpoint(control.lease, control.current["id"], None, models=3)
    original = await control.repository.get(control.current["id"])
    if phase == "dispatched":
        with pytest.raises(JournalUnavailable, match="work_response_not_persisted"):
            await before.journal.load(control.lease, original["id"], "retired-contract")
    resumed = WorkSession(control, "current-contract")
    current = TurnTranscript(
        (
            ChatMessage("system", "current contract"),
            ChatMessage("user", "current legal public input"),
        )
    )
    actual = await resumed.restore(current)
    rendered = repr(actual.request())
    assert "current legal public input" in rendered
    assert "retired-signature" not in rendered and "retired frozen body" not in rendered
    assert actual.chain_id != before.transcript.chain_id
    assert not resumed.pending and not resumed.pending_compositions
    now = await control.repository.get(original["id"])
    for key in (
        "id",
        "source_key",
        "source_json",
        "parent_work_id",
        "goal",
        "model_requests",
        "tool_calls",
        "sent_messages",
    ):
        assert now[key] == original[key]


@pytest.mark.parametrize(
    "name,arguments",
    [("retired_tool", "not current arguments"), ("find_contacts", '{"view":"entries"}')],
)
async def test_retired_long_call_uses_original_hashed_effect_key_without_replay(
    database, tmp_path, name, arguments
):
    control = await _control(database, tmp_path, worker=True)
    before = WorkSession(control, "retired-contract")
    await before.restore(TurnTranscript((ChatMessage("user", "original task"),)))
    call = ToolCall("provider-call-" + "a" * 400, ToolFunction(name, arguments))
    before.transcript.append(ChatMessage("assistant", tool_calls=(call,)))
    executions = 0

    async def original():
        nonlocal executions
        executions += 1
        return json.dumps(
            {"ok": True, "data": {"run_id": "original-external-run", "status": "succeeded"}}
        )

    await invoke_tool(before, call, original, side_effecting=False)
    original_key = before.call_key(call.id)
    assert original_key.startswith("invocation:v1:")
    await before.save("response", (call,))
    resumed = WorkSession(control, "current-contract")
    actual = await resumed.restore(TurnTranscript((ChatMessage("user", "current input"),)))
    rendered = repr(actual.request())
    assert original_key in rendered and "original-external-run" in rendered
    assert not any(
        message.tool_calls or message.role == "tool" for message in actual.request().messages
    )
    assert executions == 1 and not resumed.pending
    assert (await resumed.journal.effect_result(original_key)).find("original-external-run") >= 0


async def test_new_journal_and_invalidation_preserve_cutover_archive_and_refs(database, tmp_path):
    from sqlalchemy import select, update

    from yuki.runtime.protocol_schema import refs
    from yuki.runtime.work_schema_v1 import journal

    control = await _control(database, tmp_path, worker=True)
    old = WorkSession(control, "retired-contract")
    await old.restore(TurnTranscript((ChatMessage("user", "frozen old material"),)))
    await old.save("paired")
    identity = control.current["id"]
    async with database.immediate_session() as writer:
        original = dict(
            (await writer.execute(select(journal).where(journal.c.work_id == identity)))
            .mappings()
            .one()
        )
        original.pop("archived_journal_json")
        archived = json.dumps(original, ensure_ascii=False, separators=(",", ":"))
        await writer.execute(
            update(journal)
            .where(journal.c.work_id == identity)
            .values(archived_journal_json=archived)
        )
        original_refs = set(
            await writer.scalars(select(refs.c.sha256).where(refs.c.work_id == identity))
        )
    current = WorkSession(control, "current-contract")
    await current.restore(TurnTranscript((ChatMessage("user", "new business material"),)))
    await current.save("paired")
    async with database.read_session() as reader:
        assert (
            await reader.scalar(
                select(journal.c.archived_journal_json).where(journal.c.work_id == identity)
            )
            == archived
        )
        assert original_refs <= set(
            await reader.scalars(select(refs.c.sha256).where(refs.c.work_id == identity))
        )
    await current.journal.invalidate(control.lease, identity)
    async with database.read_session() as reader:
        frozen = (
            (await reader.execute(select(journal).where(journal.c.work_id == identity)))
            .mappings()
            .one()
        )
        assert frozen["archived_journal_json"] == archived
        assert frozen["payload_json"] == original["payload_json"]
        assert original_refs <= set(
            await reader.scalars(select(refs.c.sha256).where(refs.c.work_id == identity))
        )
    resumed = WorkSession(control, "current-contract")
    actual = await resumed.restore(TurnTranscript((ChatMessage("user", "current public context"),)))
    assert "current public context" in repr(actual.request())
    assert "frozen old material" not in repr(actual.request())
    assert not resumed.uses_recovery_transcript
