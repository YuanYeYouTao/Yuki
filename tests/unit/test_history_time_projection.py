"""Time labels belong to model-visible messages, not durable event accounting."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

from qq_ai_bot.conversation.frozen_fragments import FrozenFragments
from qq_ai_bot.conversation.rollup.prompt_accounting import (
    durable_uncovered_event_characters,
    prompt_accounting_characters,
)
from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.event_prompt import ChatEventPromptRenderer
from qq_ai_bot.persistence.repository_records import EventRecord


def _event(event_id: int, at: datetime, content: str = "消息") -> EventRecord:
    return EventRecord(
        id=event_id,
        bot_user_id="8000",
        platform_message_id=f"platform-{event_id}",
        scope_type=ScopeType.GROUP,
        sender_user_id="1001",
        sender_group_card="阿明",
        direction="inbound",
        content=content,
        visual_summary="",
        segments=(),
        occurred_at=at,
        group_id="2001",
        canonical_conversation_id="conversation-1",
    )


def test_history_time_uses_first_event_and_caps_the_whole_group_at_five_minutes() -> None:
    first = _event(6912, datetime(2026, 9, 25, 6, 2, 11, tzinfo=UTC), "今晚提醒我")
    near = _event(6913, first.occurred_at + timedelta(minutes=4, seconds=59), "八点开始")
    boundary = _event(6914, first.occurred_at + timedelta(minutes=5), "还有一件事")
    later = _event(6915, first.occurred_at + timedelta(minutes=8), "刚才说错了")

    rows = (first, near, boundary, later)
    history = ChatEventPromptRenderer(rows).main_agent_history(rows)

    assert [ids for _, ids, _ in history] == [(6912, 6913, 6914), (6915,)]
    assert history[0][2].content == (
        "[14:02:11｜阿明|QQ:1001]\n#6912>今晚提醒我\n#6913>八点开始\n#6914>还有一件事"
    )
    assert (history[1][2].content or "").startswith("[14:10:11｜阿明|QQ:1001]\n")


def test_history_starts_new_group_at_local_midnight_and_on_backward_event_time() -> None:
    first = _event(1, datetime(2026, 9, 25, 15, 59, 30, tzinfo=UTC))
    next_day = _event(2, first.occurred_at + timedelta(minutes=2))
    backward = _event(3, first.occurred_at + timedelta(minutes=1))

    rows = (first, next_day, backward)
    history = ChatEventPromptRenderer(rows).main_agent_history(rows)

    assert [ids for _, ids, _ in history] == [(1,), (2,), (3,)]
    assert [message.content.split("\n", 1)[0] for _, _, message in history] == [
        "[23:59:30｜阿明|QQ:1001]",
        "[00:01:30｜阿明|QQ:1001]",
        "[00:00:30｜阿明|QQ:1001]",
    ]


def test_backward_time_splits_a_group_even_inside_the_first_five_minutes() -> None:
    first = _event(1, datetime(2026, 9, 25, 6, 0, tzinfo=UTC))
    forward = _event(2, first.occurred_at + timedelta(minutes=4))
    backward = _event(3, first.occurred_at + timedelta(minutes=3))

    rows = (first, forward, backward)
    history = ChatEventPromptRenderer(rows).main_agent_history(rows)

    assert [ids for _, ids, _ in history] == [(1, 2), (3,)]
    assert (history[1][2].content or "").startswith("[14:03:00｜阿明|QQ:1001]\n")


def test_current_event_time_is_visible_without_changing_single_event_watermark() -> None:
    event = _event(6912, datetime(2026, 9, 25, 6, 2, 11, tzinfo=UTC), "原文")
    renderer = ChatEventPromptRenderer((event,))

    assert (
        renderer.reference_message(
            event, current_event_id=event.id, current_content="当前输入"
        ).content
        == "[14:02:11｜阿明|QQ:1001]\n#6912>当前输入"
    )
    assert renderer.render_reference_event(event) == "[阿明|QQ:1001]\n#6912>原文"
    assert durable_uncovered_event_characters(event) == len(renderer.render_reference_event(event))
    assert prompt_accounting_characters((event,)) == len(
        renderer.main_agent_history((event,))[0][2].content or ""
    )


def test_frozen_old_fragment_keeps_its_text_when_new_event_is_appended() -> None:
    first = _event(1, datetime(2026, 9, 25, 6, 2, 11, tzinfo=UTC), "旧消息")
    second = replace(first, id=2, platform_message_id="platform-2", content="新消息")
    renderer = ChatEventPromptRenderer((first, second))
    grouped = tuple(
        (ids, message) for _, ids, message in renderer.main_agent_history((first, second))
    )
    individual = tuple(
        (ids, message)
        for row in (first, second)
        for _, ids, message in renderer.main_agent_history((row,))
    )
    frozen = FrozenFragments.load(
        [
            {
                "kind": "model_input",
                "event_ids": [1],
                "message": {"role": "user", "content": "[阿明|QQ:1001]\n#1>旧消息"},
            }
        ]
    )

    extended = frozen.extend_history(grouped, individual)

    assert [message.content for message in extended.messages()] == [
        "[阿明|QQ:1001]\n#1>旧消息",
        "[14:02:11｜阿明|QQ:1001]\n#2>新消息",
    ]
