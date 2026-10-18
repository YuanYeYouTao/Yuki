"""Issue #50 regressions at the persisted-history prompt boundary."""

from __future__ import annotations

from dataclasses import asdict, replace
from datetime import UTC, datetime

import pytest

from yuki.adapters.onebot.legacy_event import project_legacy_segments
from yuki.domain.conversations import ScopeType
from yuki.event_prompt import ChatEventPromptRenderer
from yuki.persistence.repository_helpers import _event_record
from yuki.persistence.repository_records import EventRecord


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
            {"type": "at", "data": {"qq": "1001"}},
            {"type": "at", "data": {"qq": "1002"}},
            {"type": "at", "data": {"qq": "1002"}},
            {"type": "at", "data": {"qq": "all"}},
        ),
        occurred_at=datetime.now(UTC),
        group_id="2001",
        mentioned_user_ids=("1001", "1002"),
        reply_sender_user_id="1002",
    )

    projection = project_legacy_segments(event.segments, event.bot_user_id)
    assert projection is not None
    event = replace(
        event,
        projected_content=projection.text,
        projected_mentions=projection.ordered_mentions,
        mentioned_user_ids=projection.mentioned_user_ids,
    )

    member = replace(
        event,
        id=8,
        sender_user_id="1002",
        sender_group_card="小明",
        segments=(),
        content="成员历史",
    )
    rendered = ChatEventPromptRenderer(
        (member, event),
        yuki_account_ids=frozenset({"9999", "8001"}),
    ).render_reference_event(event)

    assert rendered.endswith(">[提及Yuki]和[提及远野][提及小明][提及小明][提及全体成员]")
    assert event.content == "旧的丢失提及正文"
    assert "8001" not in rendered

    base = replace(
        event,
        id=1,
        platform_message_id="same-platform-id",
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
    quoted = renderer.render_reference_event(reply)
    assert "回复:#1/" in quoted
    assert "回复:#2/" not in quoted
    missing = renderer.render_reference_event(replace(reply, reply_to_event_id=None))
    assert "回复:引用不可用" in missing


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

    with caplog.at_level("WARNING", logger="yuki.adapters.onebot.legacy_event"):
        projection = project_legacy_segments(event.segments, event.bot_user_id)
        assert projection is None
        rendered = ChatEventPromptRenderer((event,)).render_reference_event(event)

    assert rendered.endswith(">可信回退正文")
    assert "historical_segment_projection_fallback category=segment_data_invalid" in caplog.text
    assert "919191" not in caplog.text
    assert "private-message-id" not in caplog.text
    assert "private-segment-content" not in caplog.text


def test_canonical_mentions_keep_original_names_and_positions_in_every_prompt_view() -> None:
    event = EventRecord(
        id=9,
        bot_user_id="9999",
        platform_message_id="mention-message",
        platform="qq",
        scope_type=ScopeType.GROUP,
        sender_user_id="1001",
        sender_group_card="发言人",
        direction="inbound",
        content="@油桃 这句和你说，@远野 那句和你说；@油桃 再确认。",
        visual_summary="",
        segments=(
            {
                "type": "mention",
                "data": {"kind": "member", "display_name": "油桃", "person_id": "person-a"},
            },
            {"type": "text", "data": {"text": " 这句和你说，"}},
            {
                "type": "mention",
                "data": {"kind": "member", "display_name": "远野", "person_id": "person-b"},
            },
            {"type": "text", "data": {"text": " 那句和你说；"}},
            {
                "type": "mention",
                "data": {"kind": "member", "display_name": "油桃", "person_id": "person-a"},
            },
            {"type": "text", "data": {"text": " 再确认。"}},
        ),
        mentioned_user_ids=("1002", "1003"),
        mentioned_person_ids=("person-a", "person-b"),
        occurred_at=datetime.now(UTC),
        canonical_conversation_id="conversation-a",
        author_kind="person",
        author_person_id="speaker",
    )
    renamed = replace(event, id=10, sender_user_id="1002", sender_group_card="后来改名")
    renderer = ChatEventPromptRenderer((event, renamed))

    for text in (
        renderer.render_event(event),
        renderer.render_reference_event(event),
        renderer.reference_message(
            event, current_event_id=event.id, current_content=event.content
        ).content,
        renderer.main_agent_history((event,))[0][2].content,
    ):
        assert text is not None and text.endswith(event.content)
        assert "提及:油桃/person:person-a,远野/person:person-b" in text
        assert "[提及成员]" not in text and "提及Yuki" not in text
        assert "后来改名" not in text


def test_canonical_current_text_does_not_rewrite_quoted_or_literal_markers() -> None:
    event = EventRecord(
        id=1,
        bot_user_id="9999",
        platform_message_id="current",
        platform="qq",
        scope_type=ScopeType.GROUP,
        sender_user_id="1001",
        direction="inbound",
        content="@油桃 当前正文",
        visual_summary="",
        segments=(),
        mentioned_user_ids=("1002",),
        occurred_at=datetime.now(UTC),
    )
    current = "[回复的消息]\n@远野 引用正文 [提及1002]\n\n@油桃 当前正文"
    rendered = ChatEventPromptRenderer((event,)).render_reference_event(
        event, current_event_id=event.id, current_content=current
    )

    assert rendered.endswith(current)


def test_old_canonical_raw_mentions_use_original_spans_and_historical_names_only() -> None:
    old = EventRecord(
        id=1,
        bot_user_id="9999",
        platform_message_id="old-name",
        platform="qq",
        scope_type=ScopeType.GROUP,
        sender_user_id="1002",
        sender_group_card="油桃",
        direction="inbound",
        content="成员旧历史",
        visual_summary="",
        segments=(),
        occurred_at=datetime.now(UTC),
        canonical_conversation_id="conversation-a",
        author_kind="person",
        author_person_id="original-person",
    )
    segments = (
        {"type": "text", "data": {"text": "[提及1002]只是字面文本；"}},
        {"type": "at", "data": {"qq": "1002"}},
        {"type": "text", "data": {"text": " 这里，"}},
        {"type": "at", "data": {"qq": "7000"}},
        {"type": "at", "data": {"qq": "1002"}},
        {"type": "at", "data": {"qq": "9999"}},
        {"type": "at", "data": {"qq": "all"}},
        {
            "type": "yuki_context",
            "data": {"platform": "qq", "mentioned_person_ids": ["recorded-person"]},
        },
    )
    stored = replace(
        old,
        id=3,
        sender_user_id="1001",
        author_person_id="speaker-person",
        content=(
            "[提及1002]只是字面文本；[提及1002] 这里，[提及7000][提及1002][提及Yuki][提及全体成员]"
        ),
        segments=segments,
        visual_summary="摘要字面[提及1002]",
    )
    record = _event_record({**asdict(stored), "segments_json": list(segments)})
    foreign_bot = replace(old, id=2, platform="telegram", bot_user_id="7000")
    later = replace(old, id=4, sender_group_card="新绑定人", author_person_id="new-person")
    renderer = ChatEventPromptRenderer((old, foreign_bot, record, later))

    for rendered in (renderer.render_event(record), renderer.render_reference_event(record)):
        assert "[提及1002]只是字面文本；@油桃 这里，@未识别成员@油桃@Yuki@全体成员" in rendered
        assert "提及:person:recorded-person" in rendered
        assert rendered.endswith("摘要字面[提及1002]")
        assert "新绑定人" not in rendered and "person:original-person" not in rendered
    assert record.content == stored.content and record.segments == segments
    assert record.projected_mentions
    current = "[回复的消息]\n[提及1002]\n\n@新名字 当前原件"
    assert renderer.render_reference_event(
        record, current_event_id=record.id, current_content=current
    ).endswith(current)
