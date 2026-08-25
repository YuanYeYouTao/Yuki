"""Proactive OneBot gateway transport tests."""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

from qq_ai_bot.automation.gateway import OneBotProactiveGateway
from qq_ai_bot.persistence.repositories import AgentActionRepository, EventLedgerRepository


@pytest.mark.asyncio
async def test_proactive_emoji_send_uses_onebot_emoji_sub_type() -> None:
    gateway = OneBotProactiveGateway(
        bot_user_id="20001",
        creator_user_id="10001",
        automation_id=7,
        automation_run_id=11,
        ledger=cast(EventLedgerRepository, AsyncMock()),
        actions=cast(AgentActionRepository, AsyncMock()),
    )
    invoke = AsyncMock(return_value=({"message_id": 30001}, object()))
    record = AsyncMock()
    gateway._invoke = invoke  # type: ignore[method-assign]
    gateway._record_media_message = record  # type: ignore[method-assign]

    await gateway.send_emoji(
        user_id=None,
        group_id="40001",
        content=b"GIF89a",
        mime_type="image/gif",
        emoji_id="emoji-1",
        summary="测试表情",
    )

    action, params = cast(tuple[str, dict[str, Any]], invoke.await_args.args)
    assert action == "send_group_msg"
    assert params["group_id"] == "40001"
    assert params["message"] == [
        {
            "type": "image",
            "data": {"file": "base64://R0lGODlh", "sub_type": 1},
        }
    ]
    record.assert_awaited_once()
