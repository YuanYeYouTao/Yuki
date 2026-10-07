"""Real scheduled sources enter Pi/Monty and retain their original domain owner."""

import json

import pytest
from sqlalchemy import select
from tests.integration.test_automation_unified_delivery import sent, setup_run
from tests.support.codemode_cases import BINARY, requires_worker
from tests.support.parent_receipts import parent_receipts

from qq_ai_bot.automation.models import RunStatus
from qq_ai_bot.domain.messages import ChatResponse, ToolCall, ToolFunction
from qq_ai_bot.runtime.work_repository import WorkRepository
from qq_ai_bot.runtime.work_schema_v1 import effects, work
from qq_ai_bot.social.db_models import SocialOperationModel
from qq_ai_bot.social.source_keys import social_source_key

pytestmark = requires_worker


@pytest.mark.parametrize("principal", ["person", "self"])
@pytest.mark.parametrize("scenario", ["normal", "refused", "cancelled", "resumed"])
async def test_scheduled_code_preserves_source_receipts_and_reentry(
    database, tmp_path, principal, scenario
):
    case = await setup_run(
        database,
        tmp_path,
        strategy="agentic",
        principal=principal,
        worker=BINARY,
        delivery="none" if scenario == "refused" else "current_group",
    )
    code = "await yuki_send_message({'text': 'CODE_PUBLIC'})"
    if scenario == "refused":
        code = "await yuki_send_message({'text': ''})"
    if scenario == "cancelled":
        code += "\nawait yuki_send_message({'text': 'MUST_NOT_SEND'})"
    if scenario == "resumed":
        code = (
            "for i in range(36):\n"
            "    await yuki_update_short_state({'slot': 1, 'text': str(i), "
            "'expected_revision': i})\n" + code
        )
    results = []

    def respond(request):
        if len(case.provider.requests) == 1:
            return ChatResponse(
                "",
                0,
                tool_calls=(
                    ToolCall("outer", ToolFunction("execute_code", json.dumps({"code": code}))),
                ),
            )
        paired = parent_receipts(request, "outer")
        assert len(paired) == 1
        result = json.loads(paired[0])
        results.append(result)
        assert result["complete"]
        return "NO_REPLY"

    case.provider._responder = respond
    downstream = tmp_path / "independent-qq.jsonl"
    original_send = case.env.bot.call_api

    async def transport(action, **params):
        if action.startswith("send_"):
            with downstream.open("a") as output:
                output.write(json.dumps({"action": action, "params": params}) + "\n")
            if scenario == "cancelled":
                await WorkRepository(database).cancel(case.env.context.conversation_id)
        return await original_send(action, **params)

    case.env.bot.call_api = transport
    first = await case.executor.execute(case.row, case.run)
    async with database.sessions() as reader:
        rows = list(await reader.execute(select(work)))
    assert len(rows) == 1
    identity = rows[0]._mapping["id"]
    source = json.loads(rows[0]._mapping["source_json"])
    assert source["automation_run_id"] == case.run.id
    assert source["principal_kind"] == principal
    assert source["actor_person_id"] == (case.env.person if principal == "person" else None)
    if scenario == "resumed":
        assert first.status is RunStatus.RUNNING
        assert (await WorkRepository(database).get(identity))["state"] == "queued"
        assert len(case.provider.requests) == 1
        assert not downstream.exists()
        first = await case.executor.execute(case.row, case.run)
    if scenario == "cancelled":
        assert first.status is RunStatus.BLOCKED
        assert (await WorkRepository(database).get(identity))["state"] == "cancelled"
    else:
        assert first.status is RunStatus.SUCCEEDED, first
        assert (await WorkRepository(database).get(identity))["state"] == "completed"
        assert len(results) == 1
        assert len(case.provider.requests) == 2
    expected = 0 if scenario == "refused" else 1
    assert len(sent(case.env)) == expected
    assert (len(downstream.read_text().splitlines()) if downstream.exists() else 0) == expected
    before = len(case.provider.requests)
    await case.executor.execute(case.row, case.run)
    assert len(case.provider.requests) == before
    assert len(sent(case.env)) == expected
    async with database.sessions() as reader:
        receipts = list(
            await reader.scalars(
                select(effects.c.receipt_json).where(effects.c.work_id == identity)
            )
        )
        social = list(await reader.scalars(select(SocialOperationModel)))
    children = [
        json.loads(row)["invocation"] for row in receipts if "invocation" in json.loads(row)
    ]
    assert all(
        item["manifest_revision"] == case.chat.runtime.runner.main_contract.revision
        for item in children
    )
    assert len([row for row in social if row.action == "send_message"]) == expected
    original_source = social_source_key(
        f"{case.env.context.conversation_id}:execution:automation:{case.run.id}:"
        f"{source['step_id']}:{case.row.script_hash}"
    )
    for row in social:
        assert row.source_turn_id == original_source
