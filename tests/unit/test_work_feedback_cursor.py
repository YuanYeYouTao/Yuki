"""Ordered input selection and journal recovery preserve the reminder cursor."""

import json

import pytest
from sqlalchemy import func, select, update
from tests.unit.test_work_communication import append_input, control_env

from qq_ai_bot.domain.conversations import ConversationScope
from qq_ai_bot.domain.messages import ChatMessage
from qq_ai_bot.persistence.models import ChatEventModel
from qq_ai_bot.runtime.work_control import WorkControl, WorkInputsPreparing
from qq_ai_bot.runtime.work_schema_v1 import inputs, work
from qq_ai_bot.runtime.work_session import WorkSession
from qq_ai_bot.services.turn_transcript import TurnTranscript
from qq_ai_bot.services.work_reporting import append_input_feedback, initialize_input_feedback


@pytest.mark.parametrize("saved_before_crash", [False, True])
async def test_legacy_cursor_cannot_skip_an_unpaired_older_input(
    database, tmp_path, saved_before_crash
):
    env, control = await control_env(database, tmp_path)
    session = control.session = WorkSession(control, "unchanged")
    transcript = await session.restore(TurnTranscript((ChatMessage("user", "original work"),)))
    old_id, old_event = await append_input(env, control, "already-shown")
    for message in await control.take_inputs("old-attempt"):
        transcript.append(message)
    await session.save("paired")
    await control.confirm_inputs()
    # Reproduce a pre-policy checkpoint. This changes only the policy version,
    # without fabricating out-of-order input states or migrating Work ownership.
    async with database.immediate_session() as writer:
        await writer.execute(
            update(work)
            .where(work.c.id == control.current["id"])
            .values(
                checkpoint_json=func.json_remove(
                    work.c.checkpoint_json, "$.communication.input_feedback_through_id"
                )
            )
        )
    control.current = await control.repository.get(control.current["id"])
    await env.service.writer.append(
        scope=ConversationScope.group("80001", "20001"),
        platform_message_id="older-unready",
        sender_user_id="10001",
        direction="inbound",
        content="original long input",
    )
    async with database.sessions() as reader:
        event = await reader.scalar(
            select(ChatEventModel.id).where(ChatEventModel.platform_message_id == "older-unready")
        )
    older_id = await control.repository.enqueue(
        control.lease.conversation_id,
        control.lease.generation,
        "older-unready",
        kind="message",
        event_id=event,
        work_id=control.current["id"],
        ready=False,
    )
    younger_id, younger_event = await append_input(env, control, "another-ingress-source")
    assert old_id < older_id < younger_id
    with pytest.raises(WorkInputsPreparing):
        await control.take_inputs("head-not-ready")
    assert [item["id"] for item in await control.pending()] == [older_id, younger_id]
    assert await control.communication_inputs() == [{"id": old_id, "event_id": old_event}]
    await control.repository.prepare_input(older_id, {"text": "long input " + "x" * 9000})
    batch = await control.take_inputs("crashed-attempt")
    assert len(batch) == 1 and str(event) in batch[0].content
    for message in batch:
        transcript.append(message)
    assert [item["id"] for item in await control.pending()] == [younger_id]
    if saved_before_crash:
        await session.save("dispatched")
    await database.close()
    recovered = WorkControl(
        control.repository, control.lease, control.source_key, control.source, control.validate
    )
    recovered.current = await control.repository.get(control.current["id"])
    restored_session = recovered.session = WorkSession(recovered, "unchanged")
    restored = await restored_session.restore(TurnTranscript((ChatMessage("user", "new wakeup"),)))
    await initialize_input_feedback(recovered)
    assert recovered.communication["input_feedback_through_id"] == (
        older_id if saved_before_crash else old_id
    )
    expected_pending = [younger_id] if saved_before_crash else [older_id, younger_id]
    assert [item["id"] for item in await recovered.pending()] == expected_pending
    first_batch = await recovered.take_inputs("new-attempt")
    for message in first_batch:
        restored.append(message)
    watermark = await append_input_feedback(recovered, restored, 0)
    assert watermark == (younger_id if saved_before_crash else older_id)
    assert "原 Work 新输入的答复机会" in restored.request().messages[-1].content
    await restored_session.save(
        "dispatched", communication_updates={"input_feedback_through_id": watermark}
    )
    await recovered.confirm_inputs()
    if not saved_before_crash:
        batch = await recovered.take_inputs("younger-attempt")
        assert len(batch) == 1 and str(younger_event) in batch[0].content
        for message in batch:
            restored.append(message)
        watermark = await append_input_feedback(recovered, restored, watermark)
        assert watermark == younger_id
    assert not await recovered.communication_reports()
    async with database.sessions() as reader:
        states = dict((await reader.execute(select(inputs.c.id, inputs.c.state))).all())
        assert states[old_id] == "consumed"
        assert states[older_id] == "consumed"
    rendered = json.dumps([message.content for message in restored.request().messages])
    # An uncommitted old paired tail exits on normal business recovery; a
    # dispatched, staged input first resumes its exact original protocol.
    assert ("new wakeup" in rendered) is (not saved_before_crash)
