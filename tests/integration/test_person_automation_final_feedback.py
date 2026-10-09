"""Explicit Person notifications reuse bounded feedback, never implicit sends."""

import json

import pytest
from sqlalchemy import select
from tests.support.automation_unified_delivery_helpers import sent, setup_run

from qq_ai_bot.automation.models import RunStatus
from qq_ai_bot.domain.messages import ChatResponse, ToolCall, ToolFunction
from qq_ai_bot.runtime.work_schema_v1 import work


@pytest.mark.parametrize("delivery", ["current_group", "self_private"])
@pytest.mark.parametrize("completion_proposed", [False, True])
async def test_person_notification_uses_explicit_target_without_courtesy_recovery(
    database, tmp_path, delivery, completion_proposed
):
    case = await setup_run(database, tmp_path, delivery=delivery, mode="silent")
    calls = 0

    def respond(request):
        nonlocal calls
        calls += 1
        if completion_proposed and calls == 1:
            return ChatResponse(
                "",
                0,
                tool_calls=(
                    ToolCall(
                        "original-completion-proposal",
                        ToolFunction("task_control", '{"action":"complete"}'),
                    ),
                ),
            )
        if calls == (2 if completion_proposed else 1):
            args = {"text": "PUBLIC_EXPLICIT_SEND"}
            if delivery == "self_private":
                args["target"] = {"kind": "person", "subject_ref": "current_speaker"}
            return ChatResponse(
                "",
                0,
                tool_calls=(
                    ToolCall(
                        "explicit-notification", ToolFunction("send_message", json.dumps(args))
                    ),
                ),
            )
        return "INTERNAL_FINAL_MUST_NOT_AUTO_SEND"

    case.provider._responder = respond
    result = await case.executor.execute(case.row, case.run)
    assert result.status is RunStatus.SUCCEEDED, result
    assert calls == (3 if completion_proposed else 2) and len(sent(case.env)) == 1
    actions = [action for action, _ in case.env.bot.calls if action.startswith("send_")]
    assert actions == ["send_private_msg" if delivery == "self_private" else "send_group_msg"]
    assert "PUBLIC_EXPLICIT_SEND" in str(sent(case.env))
    assert "INTERNAL_FINAL" not in str(sent(case.env))
    async with database.sessions() as reader:
        sources = list(await reader.scalars(select(work.c.source_json)))
    assert any(
        json.loads(source).get("owner") == "automation"
        and json.loads(source).get("delivery_target") == delivery
        for source in sources
    )


@pytest.mark.parametrize("delivery", ["current_group", "self_private"])
async def test_unsent_person_notification_final_stops_without_courtesy_correction(
    database, tmp_path, delivery
):
    case = await setup_run(database, tmp_path, delivery=delivery, mode="silent")
    result = await case.executor.execute(case.row, case.run)
    assert result.status is not RunStatus.SUCCEEDED
    assert len(case.provider.requests) == 1 and not sent(case.env)


async def test_none_person_automation_nonempty_internal_final_is_silent(database, tmp_path):
    case = await setup_run(database, tmp_path, delivery="none", mode="silent")
    result = await case.executor.execute(case.row, case.run)
    assert result.status is RunStatus.SUCCEEDED
    assert len(case.provider.requests) == 1 and not sent(case.env)
