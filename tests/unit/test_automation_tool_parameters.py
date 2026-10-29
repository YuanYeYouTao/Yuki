"""Regress inactive arguments, finite-count updates and paged public projections."""

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from yuki.automation.models import AutomationStatus, RunStatus
from yuki.automation.tools import AutomationToolService


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
