"""Issue #50 regressions at the persisted-history prompt boundary."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.event_prompt import ChatEventPromptRenderer
from qq_ai_bot.persistence.repository_records import EventRecord


def test_history_reprojects_ordered_mentions_from_persisted_segments() -> None:
    event = EventRecord(
        id=9,
        bot_user_id="9999",
        platform_message_id="mention-message",
        scope_type=ScopeType.GROUP,
        sender_user_id="1001",
        sender_group_card="远野",
        direction="inbound",
        content="旧的丢失提及正文",
        visual_summary="",
        segments=(
            {"type": "at", "data": {"qq": "8001"}},
            {"type": "text", "data": {"text": "和"}},
            {"type": "at", "data": {"qq": "1002"}},
            {"type": "at", "data": {"qq": "1002"}},
            {"type": "at", "data": {"qq": "all"}},
        ),
        occurred_at=datetime.now(UTC),
        group_id="2001",
        mentioned_user_ids=("1002",),
    )

    rendered = ChatEventPromptRenderer(
        (event,),
        yuki_account_ids=frozenset({"9999", "8001"}),
    ).render_reference_event(event)

    assert rendered.endswith(">[提及Yuki]和[提及成员1][提及成员1][提及全体成员]")
    assert "8001" not in rendered


def test_history_segment_fallback_log_never_contains_content_or_ids(
    caplog: pytest.LogCaptureFixture,
) -> None:
    event = EventRecord(
        id=919191,
        bot_user_id="9999",
        platform_message_id="private-message-id",
        scope_type=ScopeType.GROUP,
        sender_user_id="1001",
        direction="inbound",
        content="可信回退正文",
        visual_summary="",
        segments=({"type": "at", "data": "private-segment-content"},),
        occurred_at=datetime.now(UTC),
        group_id="2001",
    )

    with caplog.at_level("WARNING", logger="qq_ai_bot.event_prompt"):
        rendered = ChatEventPromptRenderer((event,)).render_reference_event(event)

    assert rendered.endswith(">可信回退正文")
    assert "historical_segment_projection_fallback category=segment_data_invalid" in caplog.text
    assert "919191" not in caplog.text
    assert "private-message-id" not in caplog.text
    assert "private-segment-content" not in caplog.text


def test_reply_projection_uses_internal_id_even_when_platform_ids_collide() -> None:
    base = EventRecord(
        id=1,
        bot_user_id="8000",
        platform_message_id="same-platform-id",
        scope_type=ScopeType.GROUP,
        sender_user_id="1001",
        direction="inbound",
        content="first",
        visual_summary="",
        segments=(),
        occurred_at=datetime.now(UTC),
        group_id="2001",
        canonical_conversation_id="conversation-1",
    )
    collision = replace(base, id=2, sender_user_id="1002", content="second")
    reply = replace(
        base,
        id=3,
        platform_message_id="reply",
        sender_user_id="1003",
        content="reply body",
        reply_to_message_id="same-platform-id",
        reply_to_event_id=base.id,
    )
    renderer = ChatEventPromptRenderer((base, collision, reply))
    rendered = renderer.render_reference_event(reply)
    assert "回复:#1/" in rendered
    assert "回复:#2/" not in rendered
    missing = renderer.render_reference_event(replace(reply, reply_to_event_id=None))
    assert "回复:引用不可用" in missing
    assert "1002" not in missing
