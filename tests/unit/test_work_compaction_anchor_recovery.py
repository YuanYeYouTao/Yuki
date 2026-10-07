"""Real journal round trips for anchors extended by public conversation deltas."""

import json

import pytest
from sqlalchemy import select, update
from tests.support.social_identity_cases import social_env
from tests.support.work_session import WorkSession

from qq_ai_bot.conversation.canonical_db_models import CanonicalConversationModel
from qq_ai_bot.domain.messages import ChatMessage, ToolCall, ToolFunction
from qq_ai_bot.runtime.work_control import WorkControl
from qq_ai_bot.runtime.work_journal import JournalUnavailable
from qq_ai_bot.runtime.work_repository import WorkConflict, WorkRepository
from qq_ai_bot.runtime.work_schema_v1 import journal
from qq_ai_bot.services.turn_transcript import TurnTranscript


async def _staged_root(database, tmp_path, static_roles=("system",), *, stage=True):
    env = await social_env(database, tmp_path)
    repository = WorkRepository(database)
    lease = await repository.acquire(env.context.conversation_id, 1)

    async def validate():
        assert await repository.valid(lease)

    control = WorkControl(repository, lease, "public-anchor", {"trigger_event_id": 1}, validate)
    control.current = await repository.accept(
        lease,
        source_key=control.source_key,
        source=control.source,
        goal="finish the original audit",
    )
    initial = (
        *(ChatMessage(role, "fixed contract") for role in static_roles),
        ChatMessage("user", "original request"),
        ChatMessage("user", "current Work state"),
    )
    session = WorkSession(control, "same-contract")
    control.session = session
    transcript = await session.restore(TurnTranscript(initial), compaction_brief=initial[-2])
    tail = (ChatMessage("user", "a real public message"), ChatMessage("assistant", "a real reply"))
    for message in tail:
        transcript.append(message)
    await repository.checkpoint(lease, control.current["id"], None, models=3, tools=1)
    control.current = await repository.get(control.current["id"])
    if stage:
        await session.summary_source()
        await session.stage_compaction(None, retained_public=tail)
    return control, session, transcript, (*initial, *tail)


@pytest.mark.parametrize("static_roles", [("system",), ("system", "developer"), ("developer",)])
async def test_staged_public_assistant_tail_recovers_exact_anchor(database, tmp_path, static_roles):
    control, first, transcript, expected_anchor = await _staged_root(
        database, tmp_path, static_roles
    )
    identity = control.current["id"]
    original = await control.repository.get(identity)
    loaded = await first.journal.load(
        control.lease, identity, first.contract, source_control=control
    )
    assert loaded.reason == "resume"
    assert loaded.record["phase"] == "paired"
    payload = json.loads(loaded.record["payload_json"])
    assert payload["pending"] == []
    assert payload["metadata"]["compaction_anchor"]["items"][-1]["value"]["role"] == "assistant"

    resumed = WorkSession(control, first.contract)
    restored = await resumed.restore(TurnTranscript((ChatMessage("user", "new wakeup"),)))
    assert restored.chain_id == transcript.chain_id
    assert restored.request() == transcript.request()
    assert resumed.compaction_anchor.request().messages == expected_anchor
    assert resumed.progress["compaction_staging"] == first.progress["compaction_staging"]
    assert control.current["id"] == identity
    current = await control.repository.get(identity)
    assert (current["model_requests"], current["tool_calls"]) == (3, 1)
    assert current["source_json"] == original["source_json"]
    assert current["goal"] == original["goal"]
    await control.repository.release(control.lease)


async def test_public_tail_recovery_keeps_original_confirmed_effect(database, tmp_path):
    control, first, transcript, expected_anchor = await _staged_root(
        database, tmp_path, stage=False
    )
    call = ToolCall(id="original-call", function=ToolFunction("terminal_exec", "{}"))
    invocations = []

    async def invoke():
        invocations.append(call.id)
        return json.dumps({"ok": True, "data": {"run_id": "original-run", "pending": False}})

    transcript.append(ChatMessage("assistant", None, tool_calls=(call,)))
    await first.save("response", (call,))
    result = await first.execute(call, invoke)
    key = first.call_key(call.id)
    transcript.append_result(call.id, result)
    await first.save("paired")
    await first.summary_source()
    await first.stage_compaction(None, retained_public=expected_anchor[-2:])
    resumed = WorkSession(control, first.contract)
    restored = await resumed.restore(TurnTranscript((ChatMessage("user", "new wakeup"),)))
    assert invocations == [call.id]
    assert restored.request() == transcript.request()
    assert await resumed.journal.effect_state(key) == "accepted"
    assert await resumed.journal.effect_result(key) == result
    assert (await control.repository.get(control.current["id"]))["model_requests"] == 3
    await control.repository.release(control.lease)


@pytest.mark.parametrize(
    "damage",
    ["missing_value", "wrong_count", "boolean_count", "unknown_role", "nontext", "tool", "opaque"],
)
async def test_staged_anchor_real_corruption_still_blocks_recovery(database, tmp_path, damage):
    control, first, _, _ = await _staged_root(database, tmp_path)
    identity = control.current["id"]
    async with database.sessions() as reader:
        raw = await reader.scalar(
            select(journal.c.payload_json).where(journal.c.work_id == identity)
        )
    payload = await first.journal.objects.hydrate(json.loads(raw))
    anchor = payload["metadata"]["compaction_anchor"]
    last = anchor["items"][-1]
    if damage == "missing_value":
        del last["value"]
    elif damage == "wrong_count":
        anchor["messages_count"] -= 1
    elif damage == "boolean_count":
        anchor["messages_count"] = True
    elif damage == "unknown_role":
        last["value"]["role"] = "invalid"
    elif damage == "nontext":
        last["value"]["content"] = {"invalid": "not text"}
    elif damage == "tool":
        last["value"]["tool_calls"] = [
            {"id": "unpaired", "type": "function", "function": {"name": "exec", "arguments": "{}"}}
        ]
    else:
        last["value"]["response_item"] = {
            "provider": "gemini",
            "protocol": "gemini",
            "payload": {"private": "opaque"},
        }
    async with database.sessions() as writer, writer.begin():
        await writer.execute(
            update(journal)
            .where(journal.c.work_id == identity)
            .values(payload_json=json.dumps(payload))
        )
    with pytest.raises(JournalUnavailable, match="work_compaction_anchor_corrupt"):
        await WorkSession(control, first.contract).restore(
            TurnTranscript((ChatMessage("user", "new wakeup"),))
        )
    assert (await control.repository.get(identity))["model_requests"] == 3
    await control.repository.release(control.lease)


async def test_public_tail_anchor_does_not_bypass_generation_fence(database, tmp_path):
    control, first, _, _ = await _staged_root(database, tmp_path)
    async with database.sessions() as writer, writer.begin():
        await writer.execute(
            update(CanonicalConversationModel)
            .where(CanonicalConversationModel.id == control.lease.conversation_id)
            .values(generation=control.lease.generation + 1)
        )
    with pytest.raises(WorkConflict, match="work_source_generation_changed"):
        await WorkSession(control, first.contract).restore(
            TurnTranscript((ChatMessage("user", "new wakeup"),))
        )
    await control.repository.release(control.lease)
