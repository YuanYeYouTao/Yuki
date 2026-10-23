"""The two model entries preserve real automation ownership and lifecycle facts."""

import json
from dataclasses import replace

from tests.support.automation_unified_delivery_helpers import setup_run

from yuki.automation.models import AutomationStatus
from yuki.services.agent_tools import ToolRuntime


async def test_query_and_manage_use_original_task_version_owner_and_receipts(database, tmp_path):
    case = await setup_run(database, tmp_path, mode="silent")
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
    listed = await call("automation_query", view="list")
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
        "runs": [],
        "next_cursor": None,
    }
    assert not any(action.startswith("send_") for action, _ in case.env.bot.calls)
    await call("automation_manage", action="cancel", automation_id=original.id)
    repeated = await call("automation_manage", action="cancel", automation_id=original.id)
    assert repeated.request_postcondition_satisfied
    completed = await call("automation_query", view="list", status="cancelled")
    assert [row["automation_id"] for row in completed.data["tasks"]] == [original.id]
    rejected = await tools.execute("automation_query", '{"view":"create"}', runtime)
    assert rejected.error_code == "invalid_arguments"
    assert (await case.repository.get(original.id)).status is AutomationStatus.CANCELLED


async def test_global_list_over_100_remains_paged_but_full_goal_is_owner_only(database, tmp_path):
    from tests.support.canonical_identity import ensure_person

    case = await setup_run(database, tmp_path, mode="silent")
    tools = case.chat._automation_tools
    runtime = ToolRuntime(
        inbound=None,
        actor_context=replace(case.actor, execution_id="paged-directory"),
        execution_id="paged-directory",
        conversation_id=case.actor.conversation_id,
        conversation_key=case.actor.conversation_id,
    )
    for index in range(104):
        await case.service.create_task(
            {"goal": f"PRIVATE_GOAL_{index}", "trigger": {"type": "after", "seconds": 60}},
            actor=case.actor,
            conversation_key=case.actor.conversation_id,
            creation_source_key=f"page:{index}",
        )
    page = await tools.execute("automation_query", '{"view":"list","limit":102}', runtime)
    assert page.ok and len(page.data["tasks"]) == 102
    assert page.data["next_cursor"] == "102"
    assert "PRIVATE_GOAL" not in json.dumps(page.data)
    tail = await tools.execute(
        "automation_query", '{"view":"list","limit":102,"cursor":"102"}', runtime
    )
    assert tail.ok and len(tail.data["tasks"]) == 3 and tail.data["next_cursor"] is None
    identifier = tail.data["tasks"][-1]["automation_id"]
    detail = await tools.execute(
        "automation_query", json.dumps({"view": "get", "automation_id": identifier}), runtime
    )
    assert detail.ok and detail.data["task"]["goal"].startswith("PRIVATE_GOAL")
    async with database.immediate_session() as session:
        other = await ensure_person(session, "other-reader")
    outsider = replace(
        case.actor,
        person_id=other,
        user_id="other-reader",
        event_id=None,
        execution_id="other-read",
    )
    denied = await tools.execute(
        "automation_query",
        json.dumps({"view": "get", "automation_id": identifier}),
        replace(runtime, actor_context=outsider, execution_id="other-read"),
    )
    assert not denied.ok and denied.error_code == "PermissionError"


async def test_history_reads_original_run_definition_after_future_goal_update(database, tmp_path):
    case = await setup_run(
        database,
        tmp_path,
        mode="silent",
        goal="ORIGINAL_REQUEST",
        trigger={"type": "interval", "seconds": 60},
    )
    original = case.row.script
    case.clock.advance(61)
    run = await case.dispatch()
    assert run is not None
    await case.service.update_task(
        case.row.id,
        {**original, "goal": "NEW_FUTURE_REQUEST"},
        actor=case.actor,
        conversation_key=case.actor.conversation_id,
    )
    runtime = ToolRuntime(
        inbound=None,
        actor_context=replace(case.actor, execution_id="history-reader"),
        execution_id="history-reader",
        conversation_id=case.actor.conversation_id,
        conversation_key=case.actor.conversation_id,
    )
    tools = case.chat._automation_tools
    current = await tools.execute(
        "automation_query", json.dumps({"view": "get", "automation_id": case.row.id}), runtime
    )
    assert current.ok and current.data["task"]["goal"] == "NEW_FUTURE_REQUEST"
    history = await tools.execute(
        "automation_query", json.dumps({"view": "history", "automation_id": case.row.id}), runtime
    )
    assert history.ok
    assert history.data["runs"][0]["version_id"] == run.version_id
    assert history.data["runs"][0]["definition"] == original
