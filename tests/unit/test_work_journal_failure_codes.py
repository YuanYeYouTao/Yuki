"""Recovery keeps known journal reasons without exposing arbitrary exception text."""

import json

import pytest
from sqlalchemy import select
from tests.unit.test_runtime_recovery import setup

from qq_ai_bot.runtime.activation_outcome import ExitReason, classify_failure
from qq_ai_bot.runtime.work_journal import JournalUnavailable
from qq_ai_bot.runtime.work_recovery_schema import recovery
from qq_ai_bot.runtime.work_supervisor import recover_failure


@pytest.mark.parametrize(
    "code",
    [
        "work_journal_missing",
        "work_journal_corrupt",
        "work_journal_media_missing",
        "work_compaction_anchor_unavailable",
        "work_compaction_anchor_corrupt",
        "work_readonly_reuse_corrupt",
        "work_effect_media_corrupt",
        "work_effect_media_missing",
    ],
)
def test_known_journal_reason_remains_specific_and_non_retryable(code):
    failure = classify_failure(JournalUnavailable(code))
    assert (failure.code, failure.stage, failure.retryable, failure.certainty) == (
        code,
        "journal",
        False,
        "unknown",
    )


def test_unknown_journal_text_does_not_enter_durable_diagnostics():
    failure = classify_failure(JournalUnavailable("private checkpoint body or credential"))
    assert failure.code == "work_journal_unavailable"
    assert "private" not in repr(failure)


@pytest.mark.asyncio
async def test_journal_reason_persists_without_resetting_original_work(database, tmp_path):
    control = await setup(database, tmp_path)
    original_id = control.current["id"]
    await control.repository.checkpoint(control.lease, original_id, None, models=24, tools=12)
    control.current = await control.repository.get(original_id)
    outcome = await recover_failure(control, JournalUnavailable("work_compaction_anchor_corrupt"))
    assert outcome.reason is ExitReason.PAUSED
    current = await control.repository.get(original_id)
    assert (current["id"], current["model_requests"], current["tool_calls"]) == (
        original_id,
        24,
        12,
    )
    async with database.sessions() as reader:
        raw = await reader.scalar(
            select(recovery.c.failure_json).where(recovery.c.work_id == original_id)
        )
    failure = json.loads(raw)
    assert failure["code"] == "work_compaction_anchor_corrupt"
    assert failure["stage"] == "journal" and failure["retryable"] is False
