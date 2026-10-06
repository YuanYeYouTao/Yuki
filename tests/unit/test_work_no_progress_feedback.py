"""Existing pause reasons reach the original notice without exposing arbitrary text."""

import json

import pytest
from sqlalchemy import select
from tests.unit.test_runtime_recovery import setup

from qq_ai_bot.runtime.activation_outcome import (
    ExitReason,
    WorkNoProgress,
    classify_failure,
    failure_status_text,
)
from qq_ai_bot.runtime.work_recovery_schema import deliveries, recovery
from qq_ai_bot.runtime.work_supervisor import recover_failure


@pytest.mark.parametrize(
    "reason,expected",
    [
        ("work_start_not_delivered", "开始说明尚未确认送达"),
        ("interactive_work_missing_exit", "明确的完成或等待决定"),
        ("repeated_tool_results", "相同工具结果"),
    ],
)
async def test_original_pause_record_and_notice_keep_known_reason(
    database, tmp_path, reason, expected
):
    control = await setup(database, tmp_path)
    original_id = control.current["id"]
    outcome = await recover_failure(control, WorkNoProgress(reason))
    assert outcome.reason is ExitReason.NO_PROGRESS
    assert control.current["id"] == original_id and control.current["state"] == "suspended"
    async with database.sessions() as session:
        failure = await session.scalar(
            select(recovery.c.failure_json).where(recovery.c.work_id == original_id)
        )
        notice = await session.scalar(
            select(deliveries.c.payload_json).where(deliveries.c.work_id == original_id).limit(1)
        )
    assert json.loads(failure)["diagnostics"] == {"reason": reason}
    assert expected in json.loads(notice)["text"]


def test_arbitrary_pause_exception_text_is_not_published_or_persisted():
    failure = classify_failure(WorkNoProgress("private user message or credential"))
    assert failure.code == "WorkNoProgress" and not failure.retryable
    assert failure.diagnostics == {}
    assert "private" not in failure_status_text(failure)
