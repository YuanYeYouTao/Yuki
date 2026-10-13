"""Scheduled Main keeps explicit Social receipts separate from internal final text."""

import json

import pytest
from sqlalchemy import select
from tests.support.automation_unified_delivery_helpers import sent, setup_run

from qq_ai_bot.automation.models import RunStatus
from qq_ai_bot.domain.conversations import ConversationScope
from qq_ai_bot.domain.messages import ChatResponse, ToolCall, ToolFunction
from qq_ai_bot.persistence.models import ChatEventModel
from qq_ai_bot.runtime.work_schema_v1 import work
from qq_ai_bot.social.db_models import SocialOperationModel


@pytest.mark.parametrize("delivery", ["current_group", "self_private", "none"])
@pytest.mark.parametrize("accept_work", [False, True])
async def test_person_notification_uses_real_target_and_only_explicit_send(
    database, tmp_path, delivery, accept_work
):
    case = await setup_run(database, tmp_path, delivery=delivery, mode="silent")
    text = "PUBLIC_EXPLICIT_SEND"

    def respond(request):
        if len(case.provider.requests) == 1:
            calls = []
            if accept_work:
                calls.append(
                    ToolCall(
                        "accept",
                        ToolFunction("task_control", '{"action":"accept","goal":"原始完整任务"}'),
                    )
                )
            calls.append(
                ToolCall("explicit-send", ToolFunction("send_message", json.dumps({"text": text})))
            )
            return ChatResponse("", 0, tool_calls=tuple(calls))
        if accept_work:
            return ChatResponse(
                "",
                0,
                tool_calls=(
                    ToolCall(
                        "finish",
                        ToolFunction(
                            "task_control",
                            '{"action":"complete","result":"INTERNAL_FINAL_MUST_NOT_AUTO_SEND"}',
                        ),
                    ),
                ),
            )
        return "INTERNAL_FINAL_MUST_NOT_AUTO_SEND"

    case.provider._responder = respond
    case.clock.advance(2)
    result = await case.dispatch()
    assert result.status is RunStatus.SUCCEEDED and result.llm_calls == 2
    assert result.messages_sent == len(sent(case.env)) == 1
    assert "PUBLIC_EXPLICIT_SEND" in str(sent(case.env))
    assert "INTERNAL_FINAL" not in str(sent(case.env))
    assert [action for action, _ in case.env.bot.calls if action.startswith("send_")] == [
        "send_private_msg" if delivery == "self_private" else "send_group_msg"
    ]
    async with database.sessions() as reader:
        rows = list((await reader.execute(select(work))).mappings())
        assert len(rows) == int(accept_work)
        if accept_work:
            source = json.loads(rows[0]["source_json"])
            assert source["automation_run_id"] == result.id
            assert source["actor_person_id"] == case.env.person
            assert "context_profile" not in source and "step_id" not in source
            assert rows[0]["state"] == "completed"
        receipts = (await reader.scalars(select(SocialOperationModel))).all()
        assert len(receipts) == 1 and receipts[0].status == "succeeded"
        receipt = {
            column.name: getattr(receipts[0], column.name)
            for column in SocialOperationModel.__table__.columns
        }
    assert await case.dispatch() == result
    assert len(case.provider.requests) == 2 and len(sent(case.env)) == 1
    async with database.sessions() as reader:
        current = await reader.get(SocialOperationModel, receipt["id"])
        assert {
            column.name: getattr(current, column.name)
            for column in SocialOperationModel.__table__.columns
        } == receipt


@pytest.mark.parametrize("work_enabled", [False, True])
async def test_quiet_internal_completion_needs_no_work_or_transport(
    database, tmp_path, work_enabled
):
    case = await setup_run(
        database, tmp_path, delivery="none", mode="silent", work_enabled=work_enabled
    )
    case.clock.advance(2)
    result = await case.dispatch()
    assert result.status is RunStatus.SUCCEEDED and result.llm_calls == 1
    assert not sent(case.env)
    async with database.sessions() as reader:
        assert not list(await reader.execute(select(work)))


@pytest.mark.parametrize("principal", ["person", "self"])
async def test_scheduled_main_contacts_receipt_history_and_recall_share_real_social_service(
    database, tmp_path, principal
):
    case = await setup_run(database, tmp_path, principal=principal, mode="silent")
    responses = []
    contexts = []
    execute = case.env.service.execute

    async def capture(name, arguments, context):
        contexts.append((name, context))
        return await execute(name, arguments, context)

    case.env.service.execute = capture

    def respond(request):
        index = len(case.provider.requests) - 1
        if index:
            result = json.loads(request.messages[-1].content)
            responses.append(result)
            assert result["ok"], result
        if index == 0:
            name, arguments = "find_contacts", {"kind": "person", "target_id": case.env.person}
        elif index == 1:
            assert responses[-1]["data"]["target_id"] == case.env.person
            name, arguments = "send_message", {"text": "SCHEDULED_SHARED_SOCIAL"}
        elif index == 2:
            name, arguments = "read_conversation_history", {
                "operation_id": responses[-1]["data"]["operation_id"]
            }
        elif index == 3:
            name, arguments = "recall_own_message", {
                "event_id": responses[1]["data"]["event_id"]
            }
        else:
            return "NO_REPLY"
        return ChatResponse(
            "",
            0,
            tool_calls=(ToolCall(f"scheduled-{index}", ToolFunction(name, json.dumps(arguments))),),
        )

    case.provider._responder = respond
    case.clock.advance(2)
    result = await case.dispatch()
    assert result.status is RunStatus.SUCCEEDED and result.llm_calls == 5
    assert len(responses) == 4 and all(response["ok"] for response in responses)
    assert len(sent(case.env)) == 1
    assert sum(action == "get_group_msg_history" for action, _ in case.env.bot.calls) == 1
    assert sum(action == "delete_msg" for action, _ in case.env.bot.calls) == 1
    assert {name for name, _ in contexts} >= {
        "find_contacts", "send_message", "read_conversation_history", "recall_own_message"
    }
    assert all(
        context.actor.principal_kind == principal
        and context.origin == "scheduled_automation"
        and context.trigger_event_id is None
        and context.inbound is None
        and (context.automation_run_id == result.id if principal == "self" else True)
        for _, context in contexts
    )
    calls = list(case.env.bot.calls)
    assert await case.dispatch() == result
    assert case.env.bot.calls == calls and len(case.provider.requests) == 5


async def test_long_private_reminder_keeps_actual_history_parts_and_original_receipts(
    database, tmp_path
):
    text = "完整提醒原文：" + "A" * 13000 + "PRIVATE_BODY_TAIL"
    case = await setup_run(database, tmp_path, delivery="self_private", mode="silent", goal=text)
    await case.env.service.writer.append(
        scope=ConversationScope.private("80001", "10001"),
        platform_message_id="original-private-material",
        sender_user_id="10001",
        direction="inbound",
        content="ORIGINAL_PRIVATE_MATERIAL",
    )
    case.provider._responder = lambda _: (
        ChatResponse(
            "",
            0,
            tool_calls=(
                ToolCall("long-send", ToolFunction("send_message", json.dumps({"text": text}))),
            ),
        )
        if len(case.provider.requests) == 1
        else "INTERNAL_FINAL"
    )
    case.clock.advance(2)
    result = await case.dispatch()
    assert result.status is RunStatus.SUCCEEDED
    assert "ORIGINAL_PRIVATE_MATERIAL" in "\n".join(
        m.content or "" for m in case.provider.requests[0].messages
    )
    assert result.messages_sent == len(sent(case.env)) > 1
    delivered = "".join(
        segment["data"]["text"]
        for payload in sent(case.env)
        for segment in payload["message"]
        if segment["type"] == "text"
    )
    assert delivered.replace("\n", "") == text
    async with database.sessions() as reader:
        receipts = (await reader.scalars(select(SocialOperationModel))).all()
        parent = next(row for row in receipts if row.action == "send_message_sequence")
        parts = [row for row in receipts if row.action == "send_message"]
        assert parent.planned_parts == len(parts) == result.messages_sent
        assert all(
            row.status == "succeeded"
            and row.target_id == case.env.person
            and row.event_id is not None
            and row.platform_reference
            for row in parts
        )
        assert (await reader.get(ChatEventModel, parts[-1].event_id)).content.endswith(
            "PRIVATE_BODY_TAIL"
        )
        original = {
            row.id: {
                column.name: getattr(row, column.name)
                for column in SocialOperationModel.__table__.columns
            }
            for row in receipts
        }
    assert await case.dispatch() == result
    async with database.sessions() as reader:
        receipts = (await reader.scalars(select(SocialOperationModel))).all()
        assert {
            row.id: {
                column.name: getattr(row, column.name)
                for column in SocialOperationModel.__table__.columns
            }
            for row in receipts
        } == original
    assert len(case.provider.requests) == 2 and len(sent(case.env)) == result.messages_sent
