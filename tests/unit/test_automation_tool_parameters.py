"""Regress inactive arguments, finite-count updates and paged public projections."""

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from jsonschema import Draft202012Validator

from yuki.automation.models import AutomationStatus, RunStatus
from yuki.automation.tools import AutomationToolService
from yuki_plugin_sdk.testing.fake_services import FakeAutomationFacade


def fixture_runtime():
    actor = SimpleNamespace(source_key="execution:probe", person_id="person:probe")
    return SimpleNamespace(require_actor=lambda: actor, conversation_key="conversation:probe")


def record():
    return SimpleNamespace(
        id=1,
        name="自动化 #1",
        status=AutomationStatus.ACTIVE,
        timezone="Asia/Shanghai",
        schedule=SimpleNamespace(model_dump=lambda **_: {}),
        next_run_at=None,
        run_count=0,
        max_runs=None,
        version_id=1,
        script={"goal": "PRIVATE_REQUEST", "trigger": {"type": "after", "seconds": 1}},
    )


@pytest.mark.parametrize(
    "arguments",
    [
        {
            "action": "update",
            "automation_id": 1,
            "task": {"goal": "complete", "trigger": {"type": "after", "seconds": 1}},
        },
        {
            "action": "create",
            "task": {
                "goal": "complete",
                "trigger": {"type": "once", "local_datetime": "2026-10-25T01:00:00+08:00"},
                "name": None,
            },
            "max_runs": None,
        },
    ],
)
def test_fixed_schema_accepts_only_actual_operation_fields(arguments):
    tools = {tool.name: tool for tool in AutomationToolService(None).definitions()}
    Draft202012Validator(tools["automation_manage"].parameters).validate(arguments)
    invalid = {**arguments, "cursor": "0"}
    assert list(Draft202012Validator(tools["automation_manage"].parameters).iter_errors(invalid))


@pytest.mark.parametrize(
    "view,extras",
    [
        ("get", {"limit": 2}),
        ("list", {"automation_id": 1}),
        ("history", {"status": "all"}),
        ("list", {"match_task": {}}),
    ],
)
async def test_query_no_longer_silently_ignores_inactive_fields(view, extras):
    arguments = {"view": view, **extras}
    if view != "list":
        arguments["automation_id"] = 1
    tools = AutomationToolService(SimpleNamespace())
    result = await tools.execute("automation_query", json.dumps(arguments), fixture_runtime())
    assert not result.ok and result.error_code == "invalid_arguments"


async def test_update_preserves_omission_and_forwards_null_or_integer():
    service = SimpleNamespace(update_task=AsyncMock(return_value=record()))
    tools = AutomationToolService(service)
    for extra in ({}, {"max_runs": None}, {"max_runs": 7}):
        result = await tools.execute(
            "automation_manage",
            json.dumps({"action": "update", "automation_id": 1, "task": record().script, **extra}),
            fixture_runtime(),
        )
        assert result.ok
        supplied = service.update_task.await_args.kwargs
        assert ("max_runs" in supplied) == ("max_runs" in extra)
        if extra:
            assert supplied["max_runs"] == extra["max_runs"]


async def test_history_pages_actual_rows_and_preserves_delivery_unknown():
    row = record()
    history = tuple(
        SimpleNamespace(
            id=i,
            status=RunStatus.SUCCEEDED,
            version_id=10 + i,
            result_summary={"result": i},
            definition={"goal": "ORIGINAL_REQUEST", "delivery": {"target": "none"}},
            delivery={"unknown": 1},
            messages_sent=3,
            scheduled_for=datetime(2026, 10, 24, tzinfo=UTC),
            finished_at=None,
            error_category=None,
        )
        for i in range(1, 6)
    )

    async def fetch(automation_id, *, actor, limit, offset):
        return row, history[offset : offset + limit]

    service = SimpleNamespace(history=AsyncMock(side_effect=fetch))
    result = await AutomationToolService(service).execute(
        "automation_query",
        '{"view":"history","automation_id":1,"limit":2,"cursor":"1"}',
        fixture_runtime(),
    )
    assert result.ok
    assert [item["id"] for item in result.data["runs"]] == [2, 3]
    assert result.data["next_cursor"] == "3"
    assert result.data["runs"][0]["definition"] == {
        "goal": "ORIGINAL_REQUEST",
        "delivery": {"target": "none"},
    }
    assert result.data["runs"][0]["delivery"] == {"unknown": 1}
    assert result.data["runs"][0]["legacy_messages_sent"] == 3
    assert service.history.await_args.kwargs["limit"] == 3
    assert service.history.await_args.kwargs["offset"] == 1


async def test_sdk_fake_uses_real_result_identity_name_and_owner_list_contract():
    facade = FakeAutomationFacade()
    task = {"goal": "PRIVATE_REQUEST", "trigger": {"type": "after", "seconds": 1}}
    first = await facade.create_task(task, request_id="original", max_runs=7)
    data = first.data["automation"]
    assert data["task_id"] == "1" and data["name"] == "自动化 #1"
    assert "PRIVATE_REQUEST" not in json.dumps(first.model_dump())
    assert await facade.create_task(task, request_id="original", max_runs=7) == first
    with pytest.raises(ValueError, match="creation_key_conflict"):
        await facade.create_task(task, request_id="original", max_runs=None)
    updated = await facade.update_task("1", {**task, "name": "public label"})
    assert updated.data["automation"]["max_runs"] == 7
    updated = await facade.update_task("1", {**task, "name": None}, max_runs=None)
    assert updated.data["automation"]["max_runs"] is None
    assert updated.data["automation"]["name"] == "自动化 #1"
    await facade.cancel("1")
    assert await facade.list_current_owner() == ()
    with pytest.raises(ValueError, match="已经结束"):
        await facade.update_task("1", task, max_runs=None)
