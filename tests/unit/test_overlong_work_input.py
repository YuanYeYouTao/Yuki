"""Overlong messages get feedback before ordinary admission and Work staging."""

from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
from tests.conftest import MemorySender
from tests.unit.test_history_dispatch_ownership import _scene, _tool

from qq_ai_bot.llm.fake import FakeLLMProvider


@pytest.mark.parametrize("active_work", [False, True])
async def test_overlong_message_is_reported_before_work_staging(
    database, tmp_path, monkeypatch, active_work
):
    provider = FakeLLMProvider(
        lambda request: _tool(
            "task_control", {"action": "accept", "goal": "original work"}, "accept"
        )
    )
    _, harness, chat, _, message = await _scene(database, tmp_path, provider, request_limit=1)
    if active_work:
        await harness.processor.handle(message, MemorySender())
    before = len(provider.requests)
    stage = AsyncMock(side_effect=AssertionError("overlong input entered Work staging"))
    monkeypatch.setattr(chat, "stage_work_input", stage)
    sender = MemorySender()
    result = await harness.processor.handle(
        replace(
            message, message_id="overlong", text="x" * (harness.settings.max_input_characters + 1)
        ),
        sender,
    )
    assert result.reason == "input_too_long"
    assert len(sender.messages) == 1
    assert "消息过长" in sender.messages[0].text
    assert len(provider.requests) == before
    stage.assert_not_awaited()
