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

    async def call(tool, /, **arguments):
        result = await tools.execute(tool, json.dumps(arguments), runtime)
        assert result.ok, result.model_payload()
        return result

    created = await call("manage_schedule", action="create", **case.row.script)
    assert created.data["id"] == case.row.id
    assert created.data["confirmation"] == "persisted"
    listed = await call("get_schedules")
    assert [row["id"] for row in listed.data["schedules"]] == [case.row.id]
    original = await case.repository.get(case.row.id)
    updated = await call(
        "manage_schedule",
        action="update",
        id=case.row.id,
        **{**case.row.script, "name": "same owned schedule, new version"},
    )
    assert updated.data["id"] == original.id
    current = await case.repository.get(original.id)
    assert current.version_id != original.version_id
    assert current.creator_user_id == original.creator_user_id
    assert (await call("get_schedules", id=original.id)).data["id"] == original.id
    await call("manage_schedule", action="pause", id=original.id)
    assert (await case.repository.get(original.id)).status is AutomationStatus.PAUSED
    await call("manage_schedule", action="resume", id=original.id)
    assert (await case.repository.get(original.id)).status is AutomationStatus.ACTIVE
    await call("manage_schedule", action="run_now", id=original.id)
    # Admission schedules work; it must not manufacture a successful execution.
    assert (await call("get_schedules", id=original.id, history=True)).data == {
        "runs": [],
        "next_cursor": None,
    }
    assert not any(action.startswith("send_") for action, _ in case.env.bot.calls)
    await call("manage_schedule", action="cancel", id=original.id)
    repeated = await call("manage_schedule", action="cancel", id=original.id)
    assert repeated.request_postcondition_satisfied
    completed = await call("get_schedules")
    assert [(row["id"], row["status"]) for row in completed.data["schedules"]] == [
        (original.id, "cancelled")
    ]
    rejected = await tools.execute("get_schedules", '{"view":"create"}', runtime)
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
    created = []
    for index in range(104):
        created.append(
            await case.service.create_task(
                {"goal": f"PRIVATE_GOAL_{index}", "trigger": {"type": "after", "seconds": 60}},
                actor=case.actor,
                conversation_key=case.actor.conversation_id,
                creation_source_key=f"page:{index}",
            )
        )
    page = await tools.execute("get_schedules", '{"limit":102}', runtime)
    assert page.ok and len(page.data["schedules"]) == 102
    assert page.data["next_cursor"] == "102"
    assert "PRIVATE_GOAL" not in json.dumps(page.data)
    tail = await tools.execute("get_schedules", '{"limit":102,"cursor":"102"}', runtime)
    assert tail.ok and len(tail.data["schedules"]) == 3 and tail.data["next_cursor"] is None
    identifier = created[-1].id
    detail = await tools.execute("get_schedules", json.dumps({"id": identifier}), runtime)
    assert detail.ok and detail.data["goal"].startswith("PRIVATE_GOAL")
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
        "get_schedules",
        json.dumps({"id": identifier}),
        replace(runtime, actor_context=outsider, execution_id="other-read"),
    )
    assert not denied.ok and denied.error_code == "permission_denied"


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
    current = await tools.execute("get_schedules", json.dumps({"id": case.row.id}), runtime)
    assert current.ok and current.data["goal"] == "NEW_FUTURE_REQUEST"
    history = await tools.execute(
        "get_schedules", json.dumps({"id": case.row.id, "history": True}), runtime
    )
    assert history.ok
    assert history.data["runs"][0]["version_id"] == run.version_id
    assert history.data["runs"][0]["definition"] == original
