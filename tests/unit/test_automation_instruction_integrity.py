"""Complete scheduled requests reach the real common Main provider payload."""

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import select
from tests.support.automation_unified_delivery_helpers import setup_run

from yuki.automation.models import DailySchedule, OnceSchedule, RunStatus
from yuki.automation.task_spec import TaskSpec, canonical_task_hash
from yuki.llm.gemini import GeminiProvider
from yuki.persistence.models import AutomationVersionModel
from yuki.runtime.work_schema_v1 import work
from yuki.time.schedules import _resolve_local, initial_run_at, next_run_at


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


async def test_long_task_persists_and_reaches_original_main_agent(database, tmp_path, gemini_wire):
    provider, requests = gemini_wire
    goal = "完整目标：" + "待处理资料" * 600
    goal += "\n占位资料，不要输出：" + "A" * 5600
    goal += "\nTAIL_MARKER=YUKI_AUTO_TAIL_OK_731\n" + "\n".join(f"条件 {i}" for i in range(13))
    case = await setup_run(database, tmp_path, provider=provider, goal=goal)
    instruction = TaskSpec.model_validate(case.row.script).instruction()
    assert instruction == goal
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
    assert submitted[0].endswith(goal)
    assert submitted[0].count(goal) == 1
    async with database.read_session() as reader:
        saved = await reader.get(AutomationVersionModel, result.version_id)
        assert json.loads(saved.script_json) == updated.script
        assert saved.script_hash == canonical_task_hash(TaskSpec.model_validate(updated.script))
        assert not list(await reader.scalars(select(work.c.id)))
    assert not [action for action, _ in case.env.bot.calls if action.startswith("send_")]
    assert await case.dispatch() == result
    assert len(requests) == 1


async def test_due_http_retry_keeps_original_runtime_context_bytes(database, tmp_path):
    requests = []
    case = None

    def receive(request):
        requests.append(request.content)
        if len(requests) == 1:
            case.clock.advance(10)
            return httpx.Response(503, json={"error": {"message": "temporary upstream failure"}})
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "content": {"role": "model", "parts": [{"text": "内部任务结束"}]},
                        "finishReason": "STOP",
                    }
                ]
            },
        )

    async with httpx.AsyncClient(
        base_url="https://wire.test/v1beta/", transport=httpx.MockTransport(receive)
    ) as client:
        provider = GeminiProvider(
            base_url="https://wire.test/v1beta/",
            api_key="offline-test",
            timeout_seconds=10,
            max_retries=1,
            client=client,
        )
        case = await setup_run(database, tmp_path, provider=provider, goal="静默处理原定目标")
        case.clock.advance(2)
        result = await case.dispatch()
    assert result.status is RunStatus.SUCCEEDED
    assert len(requests) == 2 and requests[0] == requests[1]
    assert b"<runtime_context>" in requests[0]


@pytest.mark.parametrize(
    "retired",
    [
        {"strategy": "static"},
        {"context": {"scene": "none"}},
        {"steps": []},
        {"limits": {"max_llm_calls": 1}},
        {"delivery": {"target": "none", "text": "旧旁路"}},
        {"constraints": ["独立要求"]},
        {"version": 1},
        {"timezone": "UTC"},
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


def test_once_offset_preserves_subsecond_instant():
    value = datetime(2026, 10, 11, 8, 0, 0, 750000, tzinfo=UTC)
    schedule = OnceSchedule(type="once", local_datetime=value)
    expected = value
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

    from yuki.automation.models import Schedule

    now = datetime(2026, 10, 11, tzinfo=UTC)
    schedule = TypeAdapter(Schedule).validate_python(declaration)
    assert initial_run_at(schedule, now, " UTC ") == (
        now + timedelta(seconds=declaration["seconds"])
        if "seconds" in declaration
        else now.replace(hour=9)
    )
