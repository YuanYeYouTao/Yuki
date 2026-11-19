"""Correctable subtask arguments retain the original native dispatch and receipt."""

import pytest
from tests.integration.test_external_agent_bridge import (
    NativeComputer,
    accepted_work,
    directory,
    settled,
)
from tests.integration.test_external_agent_bridge import bridge as bridge_fixture

from yuki.runtime.work_control import WorkControl
from yuki.runtime.work_repository import WorkRepository

bridge = bridge_fixture


@pytest.mark.parametrize(
    "wrong_reply,expected",
    [
        ("not-a-permission", "permission_id_required"),
        ("progress-event", "external_agent_question_not_found"),
    ],
)
async def test_subtask_argument_errors_can_be_corrected_without_duplicate_native_dispatch(
    bridge, wrong_reply, expected
):
    computer = await NativeComputer(bridge).start()
    try:
        instance, session = await directory(bridge)
        start = {
            "goal": "test",
            "executor": {"agent": instance},
            "session": session,
            "relationship": "takeover",
        }
        repository = WorkRepository(bridge.database)
        lease = await repository.acquire(bridge.env.context.conversation_id, 1)

        async def valid():
            assert await repository.valid(lease)

        idle = WorkControl(repository, lease, "no-current-task", bridge.source, valid)
        denied = await idle.tool_outcome("start_subtask", start, "original-start")
        await repository.release(lease)
        assert denied.error_code == "subtask_requires_current_task"
        assert "manage_task(action=start)" in denied.public_message
        assert not any(command["action"] == "prompt" for command in computer.commands)
        async with accepted_work(bridge) as control:
            original = await control.tool_outcome("start_subtask", start, "original-start")
            assert original.ok, original
            await settled(bridge)
            # The same call names the same record and the same native operation; the
            # dispatch status is the original receipt as it stands now.
            replay = await control.tool_outcome("start_subtask", start, "original-start")
            assert replay.data["id"] == original.data["id"]
            assert (
                replay.data["dispatch"]["operation_id"] == original.data["dispatch"]["operation_id"]
            )
            identity = original.data["id"]
            [prompt] = [command for command in computer.commands if command["action"] == "prompt"]
            await computer.event(
                prompt["id"],
                "permission",
                {"native_request_id": 0, "connection_id": "original-native-connection"},
            )
            await computer.event(prompt["id"], "progress", {"text": "reading"})
            await settled(bridge)
            observed = await control.tool_outcome("get_tasks", {"id": identity}, "read-original")
            [pending] = observed.data["card"]["pending"]
            reply_to = pending["permission_id"]
            response = {"id": identity, "answer": "accept", "text": "expected"}
            # A real event of this record that is not a request, or not an event ID at all.
            response["reply_to"] = (
                str(int(reply_to) + 1) if wrong_reply == "progress-event" else wrong_reply
            )
            missing = await control.tool_outcome("message_task", response, "original-response")
            assert missing.error_code == expected
            assert "reply_to" in missing.public_message
            assert not any(command["action"] == "respond" for command in computer.commands)
            response["reply_to"] = reply_to
            answered = await control.tool_outcome("message_task", response, "original-response")
            assert answered.ok, answered
            await settled(bridge)
            repeated = await control.tool_outcome("message_task", response, "original-response")
            assert (repeated.data["id"], repeated.data["answered"]) == (identity, reply_to)
            assert answered.data["answered"] == reply_to
            await settled(bridge)
        assert [command["action"] for command in computer.commands].count("prompt") == 1
        assert [command["action"] for command in computer.commands].count("respond") == 1
        native = next(command for command in computer.commands if command["action"] == "respond")
        assert native["arguments"]["native_request_id"] == 0
        assert native["arguments"]["operation_id"] == prompt["id"]
    finally:
        await computer.close()
