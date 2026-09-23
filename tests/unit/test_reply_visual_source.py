"""A quoted image cannot choose its ledger owner through a QQ message ID."""

from __future__ import annotations

from dataclasses import replace
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
from qq_ai_bot.services import processor as processor_module
from qq_ai_bot.services.processor import MessageProcessor


@pytest.mark.asyncio
async def test_quoted_image_uses_resolved_internal_reference(monkeypatch) -> None:
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
    monkeypatch.setattr(processor_module, "compact_visual_summary", lambda _value: "summary")
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
        reply_to_event_id=77,
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
    assert result.observation is observation
    ledger.get_reply_event.assert_awaited_once_with(
        77, conversation_id="conversation-1", current_generation_only=True
    )
    assert processor._vision.analyze.await_args.kwargs["source_event_id"] == 77
    ledger.set_visual_summary.assert_awaited_once_with(77, "summary")

    ledger.get_reply_event.reset_mock()
    ledger.set_visual_summary.reset_mock()
    old_record = replace(message, reply_to_event_id=None)
    await processor._analyze_visual_input(message=old_record, **arguments)
    ledger.get_reply_event.assert_not_awaited()
    ledger.set_visual_summary.assert_awaited_once_with(99, "summary")
