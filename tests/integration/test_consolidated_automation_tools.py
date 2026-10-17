"""The two model entries preserve real automation ownership and lifecycle facts."""

import json
from dataclasses import replace

from tests.support.automation_unified_delivery_helpers import setup_run

from yuki.automation.models import AutomationStatus
from yuki.services.agent_tools import ToolRuntime


async def test_query_and_manage_use_original_task_version_owner_and_receipts(database, tmp_path):
    case = await setup_run(database, tmp_path, delivery="none")
    tools = case.chat._automation_tools
    runtime = ToolRuntime(
        inbound=None,
        actor_context=replace(case.actor, execution_id="public-automation"),
        execution_id="public-automation",
        conversation_id=case.actor.conversation_id,
        conversation_key=case.actor.conversation_id,
    )

    async def call(name, **arguments):
        result = await tools.execute(name, json.dumps(arguments), runtime)
        assert result.ok, result.model_payload()
        return result

    created = await call("automation_manage", action="create", task=case.row.script)
    assert created.data["automation_id"] == case.row.id
    assert created.data["confirmation"] == "persisted"
    listed = await call("automation_query", view="list", match_task=case.row.script)
    assert [row["automation_id"] for row in listed.data["tasks"]] == [case.row.id]
    original = await case.repository.get(case.row.id)
    updated = await call(
        "automation_manage",
        action="update",
        automation_id=case.row.id,
        task={**case.row.script, "name": "same owned schedule, new version"},
    )
    assert updated.data["automation_id"] == original.id
    current = await case.repository.get(original.id)
    assert current.version_id != original.version_id
    assert current.creator_user_id == original.creator_user_id
    assert (await call("automation_query", view="get", automation_id=original.id)).data[
        "automation_id"
    ] == original.id
    await call("automation_manage", action="pause", automation_id=original.id)
    assert (await case.repository.get(original.id)).status is AutomationStatus.PAUSED
    await call("automation_manage", action="resume", automation_id=original.id)
    assert (await case.repository.get(original.id)).status is AutomationStatus.ACTIVE
    await call("automation_manage", action="run_now", automation_id=original.id)
    # Admission schedules work; it must not manufacture a successful execution.
    assert (await call("automation_query", view="history", automation_id=original.id)).data == {
        "runs": []
    }
    assert not any(action.startswith("send_") for action, _ in case.env.bot.calls)
    await call("automation_manage", action="cancel", automation_id=original.id)
    repeated = await call("automation_manage", action="cancel", automation_id=original.id)
    assert repeated.request_postcondition_satisfied
    completed = await call("automation_query", view="completed")
    assert [row["automation_id"] for row in completed.data["completed_history"]] == [original.id]
    assert "recent_creation_outcomes" in (await call("automation_query", view="diagnose")).data
    rejected = await tools.execute("automation_query", '{"view":"create"}', runtime)
    assert rejected.error_code == "invalid_arguments"
    assert (await case.repository.get(original.id)).status is AutomationStatus.CANCELLED
