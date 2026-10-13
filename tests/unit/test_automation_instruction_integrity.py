"""Complete scheduled requests reach the real common Main provider payload."""

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import select
from tests.support.automation_unified_delivery_helpers import setup_run

from qq_ai_bot.automation.models import DailySchedule, OnceSchedule, RunStatus
from qq_ai_bot.automation.task_spec import TaskSpec, canonical_task_hash
from qq_ai_bot.llm.gemini import GeminiProvider
from qq_ai_bot.persistence.models import AutomationModel, AutomationVersionModel
from qq_ai_bot.runtime.work_budget_schema import automation_budgets
from qq_ai_bot.runtime.work_schema_v1 import work
from qq_ai_bot.time.schedules import _resolve_local, initial_run_at, next_run_at


@pytest.fixture
async def gemini_wire():
    requests = []

    def receive(request):
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "content": {"role": "model", "parts": [{"text": "INTERNAL_COMPLETE"}]},
                        "finishReason": "STOP",
                    }
                ]
            },
        )

    base_url = "https://wire.test/v1beta/"
    async with httpx.AsyncClient(
        base_url=base_url, transport=httpx.MockTransport(receive)
    ) as client:
        yield (
            GeminiProvider(
                base_url=base_url,
                api_key="offline-test",
                timeout_seconds=10,
                max_retries=0,
                client=client,
            ),
            requests,
        )


@pytest.mark.parametrize("padding", [32, 4300, 5600])
async def test_long_task_persists_and_reaches_original_main_agent(
    database, tmp_path, padding, gemini_wire
):
    provider, requests = gemini_wire
    goal = "完整目标：" + "待处理资料" * 600
    constraints = [
        "占位资料，不要输出：" + "A" * padding + "\nTAIL_MARKER=YUKI_AUTO_TAIL_OK_731",
        *[f"条件 {i}" for i in range(13)],
    ]
    case = await setup_run(
        database, tmp_path, provider=provider, delivery="none", goal=goal, constraints=constraints
    )
    expected = {"goal": goal, "constraints": constraints, "delivery": "none"}
    instruction = TaskSpec.model_validate(case.row.script).instruction()
    assert json.loads(instruction) == expected
    original_due = case.row.next_run_at
    updated = await case.service.update_task(
        case.row.id,
        {**case.row.script, "name": "完整任务" * 40},
        actor=case.actor,
        conversation_key="group:80001:20001",
    )
    assert updated.next_run_at == original_due
    case.clock.advance(2)
    result = await case.dispatch()
    assert result.status is RunStatus.SUCCEEDED and result.llm_calls == 1
    assert result.version_id == updated.version_id
    assert len(requests) == 1
    submitted = [
        part["text"]
        for message in requests[0]["contents"]
        for part in message["parts"]
        if message["role"] == "user" and "text" in part and instruction in part["text"]
    ]
    assert len(submitted) == 1
    declaration, _ = json.JSONDecoder().raw_decode(submitted[0], submitted[0].index(instruction))
    assert declaration == expected
    assert declaration["constraints"][0].endswith("\nTAIL_MARKER=YUKI_AUTO_TAIL_OK_731")
    async with database.sessions() as reader:
        stored = await reader.get(AutomationModel, case.row.id)
        saved = await reader.get(AutomationVersionModel, result.version_id)
        assert json.loads(stored.script_json) == json.loads(saved.script_json) == updated.script
        assert saved.script_hash == canonical_task_hash(TaskSpec.model_validate(updated.script))
        assert not list(await reader.scalars(select(work.c.id)))
        assert not list(await reader.execute(select(automation_budgets)))
    assert not [action for action, _ in case.env.bot.calls if action.startswith("send_")]
    assert await case.dispatch() == result
    assert len(requests) == 1


@pytest.mark.parametrize(
    "retired",
    [
        {"strategy": "static"},
        {"context": {"scene": "none"}},
        {"steps": []},
        {"limits": {"max_llm_calls": 1}},
        {"delivery": {"target": "none", "text": "旧旁路"}},
    ],
)
def test_future_task_schema_has_only_timing_target_and_complete_request(retired):
    with pytest.raises(ValidationError):
        TaskSpec.model_validate(
            {
                "name": "未来任务",
                "goal": "完整目标",
                "trigger": {"type": "after", "seconds": 1},
                **retired,
            }
        )


def test_naive_once_and_local_iso_preserve_subsecond_instant():
    value = datetime(2026, 10, 11, 8, 0, 0, 750000)
    schedule = OnceSchedule(type="once", local_datetime=value, timezone=" UTC ")
    expected = value.replace(tzinfo=UTC)
    assert initial_run_at(schedule, expected.replace(microsecond=500000), "UTC") == expected
    assert _resolve_local(value, " UTC ") == expected
    assert _resolve_local(expected, "UTC") == expected


def test_daily_schedule_uses_original_iana_skipped_day():
    schedule = DailySchedule(type="daily", hour=9, minute=0, timezone="Pacific/Apia")
    after = datetime(2011, 12, 29, 20, tzinfo=UTC)
    assert next_run_at(schedule, after, "UTC") == datetime(2011, 12, 30, 19, tzinfo=UTC)


@pytest.mark.parametrize(
    ("local", "expected"),
    [
        (datetime(2026, 3, 8, 2, 30), datetime(2026, 3, 8, 7, 30, tzinfo=UTC)),
        (datetime(2026, 11, 1, 1, 30), datetime(2026, 11, 1, 5, 30, tzinfo=UTC)),
    ],
)
def test_original_iana_gap_and_first_fold(local, expected):
    assert _resolve_local(local, "America/New_York") == expected


@pytest.mark.parametrize(
    "declaration",
    [
        {"type": "after", "seconds": 2 * 365 * 86400},
        {"type": "interval", "seconds": 2 * 365 * 86400},
        {"type": "weekly", "hour": 9, "minute": 0, "weekdays": [7] * 8},
    ],
)
def test_declared_long_schedule_and_repeated_weekday_use_original_calendar(declaration):
    from pydantic import TypeAdapter

    from qq_ai_bot.automation.models import Schedule

    now = datetime(2026, 10, 11, tzinfo=UTC)
    schedule = TypeAdapter(Schedule).validate_python(declaration)
    assert initial_run_at(schedule, now, " UTC ") == (
        now + timedelta(seconds=declaration["seconds"])
        if "seconds" in declaration
        else now.replace(hour=9)
    )
