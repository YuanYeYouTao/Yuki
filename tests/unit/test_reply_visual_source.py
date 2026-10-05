"""Quoted images cannot revive the implicit auxiliary vision/summary path."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.domain.messages import (
    AttachmentKind,
    InboundMessage,
    MessageAttachment,
    SenderIdentity,
)
from qq_ai_bot.services.processor import MessageProcessor


@pytest.mark.asyncio
@pytest.mark.parametrize("reference", [77, None])
async def test_quoted_image_without_native_support_never_calls_auxiliary_vision(reference) -> None:
    processor = object.__new__(MessageProcessor)
    processor._native_images = None
    processor._settings = SimpleNamespace(vision_enabled=True)
    observation = SimpleNamespace()
    processor._vision = SimpleNamespace(analyze=AsyncMock(return_value=observation))
    ledger = SimpleNamespace(
        get_reply_event=AsyncMock(return_value=SimpleNamespace(id=77)),
        set_visual_summary=AsyncMock(),
    )
    processor._ledger = ledger
    message = InboundMessage(
        message_id="current-platform",
        event_type="message",
        scope_type=ScopeType.GROUP,
        sender=SenderIdentity(user_id="1001"),
        text="看引用图片",
        bot_user_id="8000",
        group_id="2001",
        reply_attachments=(
            MessageAttachment(kind=AttachmentKind.IMAGE, label="image", source="reply"),
        ),
        reply_to_message_id="colliding-platform-id",
        reply_to_event_id=reference,
        conversation_id="conversation-1",
        received_at=datetime.now(UTC),
    )
    arguments = dict(
        question="describe",
        source_event_id=99,
        conversation_key="conversation-1",
        event_key="event:99",
        sender=object(),
        runtime=SimpleNamespace(vision=object()),
    )
    result = await processor._analyze_visual_input(message=message, **arguments)
    assert result.observation is None and result.failed
    assert result.error_code == "image_capability_unavailable"
    assert "未读取" in result.attachment_text
    processor._vision.analyze.assert_not_awaited()
    ledger.get_reply_event.assert_not_awaited()
    ledger.set_visual_summary.assert_not_awaited()
