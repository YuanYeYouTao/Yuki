"""Explicit Person notifications reuse bounded feedback, never implicit sends."""

import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from tests.integration.test_automation_unified_delivery import sent, setup_run

from qq_ai_bot.automation.models import RunStatus
from qq_ai_bot.domain.messages import ChatResponse, ToolCall, ToolFunction
from qq_ai_bot.runtime.work_schema_v1 import work
from qq_ai_bot.services.main_agent_backend import MainAgentBackend, UnsentFinalResponseError


@pytest.mark.parametrize("delivery", ["current_group", "self_private"])
@pytest.mark.parametrize("completion_proposed", [False, True])
async def test_person_notification_internal_final_gets_one_correct_target_opportunity(
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
        if calls == (3 if completion_proposed else 2):
            feedback = request.messages[-1].content
            assert "明确要求通知" in feedback
            args = {"text": "PUBLIC_AFTER_CORRECTION"}
            if delivery == "self_private":
                assert "不能改发当前群" in feedback
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
    assert calls == (4 if completion_proposed else 3) and len(sent(case.env)) == 1
    actions = [action for action, _ in case.env.bot.calls if action.startswith("send_")]
    assert actions == ["send_private_msg" if delivery == "self_private" else "send_group_msg"]
    assert "PUBLIC_AFTER_CORRECTION" in str(sent(case.env))
    assert "INTERNAL_FINAL" not in str(sent(case.env))
    async with database.sessions() as reader:
        sources = list(await reader.scalars(select(work.c.source_json)))
    assert any(
        json.loads(source).get("owner") == "automation"
        and json.loads(source).get("delivery_target") == delivery
        for source in sources
    )


@pytest.mark.parametrize("delivery", ["current_group", "self_private"])
async def test_repeated_person_notification_final_stops_after_one_correction(
    database, tmp_path, delivery
):
    case = await setup_run(database, tmp_path, delivery=delivery, mode="silent")
    result = await case.executor.execute(case.row, case.run)
    assert result.status is not RunStatus.SUCCEEDED
    assert len(case.provider.requests) == 2 and not sent(case.env)


async def test_none_person_automation_nonempty_internal_final_is_silent(database, tmp_path):
    case = await setup_run(database, tmp_path, delivery="none", mode="silent")
    result = await case.executor.execute(case.row, case.run)
    assert result.status is RunStatus.SUCCEEDED
    assert len(case.provider.requests) == 1 and not sent(case.env)


@pytest.mark.parametrize(
    "source,child,attempted,sent_count,expected",
    [
        (
            {"owner": "automation", "principal_kind": "person", "delivery_target": "current_group"},
            False,
            False,
            0,
            True,
        ),
        (
            {"owner": "automation", "principal_kind": "person", "delivery_target": "none"},
            False,
            False,
            0,
            False,
        ),
        (
            {
                "owner": "plugin_invocation",
                "principal_kind": "person",
                "delivery_target": "current_group",
            },
            False,
            False,
            0,
            False,
        ),
        ({"owner": "automation", "principal_kind": "person"}, False, False, 0, False),
        (
            {"owner": "automation", "principal_kind": "person", "delivery_target": "current_group"},
            True,
            False,
            0,
            False,
        ),
        (
            {"owner": "automation", "principal_kind": "person", "delivery_target": "current_group"},
            False,
            True,
            0,
            False,
        ),
        (
            {"owner": "automation", "principal_kind": "person", "delivery_target": "current_group"},
            False,
            False,
            1,
            False,
        ),
    ],
)
def test_notification_feedback_requires_original_trusted_policy(
    source, child, attempted, sent_count, expected
):
    from qq_ai_bot.runtime.origin import TurnOrigin

    backend = object.__new__(MainAgentBackend)
    backend._send_message_attempted = attempted
    backend.messages_sent = sent_count
    backend._unsent_final_feedback_count = 0
    backend._capability_was_used = False
    runtime = SimpleNamespace(
        origin=TurnOrigin.SCHEDULED_AUTOMATION,
        delegated_authority=SimpleNamespace(principal_kind="person"),
        work_control=SimpleNamespace(
            source=source, lease=SimpleNamespace(work_id="child" if child else None)
        ),
        invocation_source={
            "owner": "automation",
            "principal_kind": "person",
            "delivery_target": "self_private",
        },
    )
    assert bool(backend.response_feedback("internal final", runtime)) is expected
    if expected:
        with pytest.raises(UnsentFinalResponseError):
            backend.response_feedback("second final", runtime)
